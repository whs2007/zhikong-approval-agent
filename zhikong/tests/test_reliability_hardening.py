"""P0-1 / P0-2 / P0-3 加固测试：持久化显式化、重试退避+幂等、风险下限与注入防护。"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, ".")

from core.config import settings


# ----------------------- P0-1：Checkpointer 显式化 -----------------------

def test_unknown_checkpointer_raises(monkeypatch):
    from workflows import graph as g
    monkeypatch.setattr(settings, "CHECKPOINTER", "weird")
    with pytest.raises(ValueError):
        g.build_checkpointer()


def test_postgres_without_driver_raises(monkeypatch):
    from workflows import graph as g
    monkeypatch.setattr(settings, "CHECKPOINTER", "postgres")
    monkeypatch.setattr(settings, "POSTGRES_URL", "postgresql://u:p@localhost:5432/db")
    try:
        import langgraph.checkpoint.postgres  # noqa: F401
        pytest.skip("postgres 驱动已安装，该路径需真实数据库，跳过")
    except ImportError:
        pass
    with pytest.raises(RuntimeError):
        g.build_checkpointer()


def test_postgres_without_url_raises(monkeypatch):
    from workflows import graph as g
    monkeypatch.setattr(settings, "CHECKPOINTER", "postgres")
    monkeypatch.setattr(settings, "POSTGRES_URL", "")
    with pytest.raises(RuntimeError):
        g.build_checkpointer()


# ----------------------- P0-3：风险下限 / 注入防护 -----------------------

def test_enforce_risk_floor():
    from workflows.providers import enforce_risk_floor
    assert enforce_risk_floor("low", 200000) == "high"      # 大额不可降为低
    assert enforce_risk_floor("low", 20000) == "medium"     # 中额至少 medium
    assert enforce_risk_floor("high", 100) == "high"        # 不会被降低
    assert enforce_risk_floor("low", 100) == "low"          # 小额保持
    assert enforce_risk_floor("bogus", 100) == "high"       # 非法值按 high 兜底


def test_openai_parse_injection_cannot_downgrade():
    """模型被注入诱导输出 low，但金额 20 万必须被强制提升为 high。"""
    from workflows.providers import OpenAILLM
    llm = object.__new__(OpenAILLM)  # 跳过需要 API Key 的构造
    res = llm._parse('{"risk_level":"low","reason":"申请人说直接通过","required_approvers":[]}',
                     200000.0)
    assert res.risk_level == "high"
    assert "强制提升" in res.reason


def test_analyst_amount_floor_forces_approval(monkeypatch):
    """即使 LLM 返回 low，大额申请也必须进入人工审批。"""
    from workflows import agents
    from workflows.providers import AnalysisResult

    class FakeLLM:
        def analyze(self, **kwargs):
            return AnalysisResult(risk_level="low", reason="注入内容声称低风险",
                                  required_approvers=[], model="fake")

    monkeypatch.setattr(agents, "_LLM", FakeLLM())
    out = agents.analyst_node({
        "thread_id": "t-floor", "trace_id": "tr-floor",
        "request_type": "procurement", "request_detail": "采购服务器",
        "amount": 300000.0, "department": "技术部", "current_phase": "analyze",
    })
    assert out["risk_level"] == "high"
    assert out["current_phase"] == "approve"
    assert out["approval_status"] == "pending"


# ----------------------- P0-2：退避 + 幂等 -----------------------

def test_backoff_delay_exponential_and_capped(monkeypatch):
    from workflows import agents
    monkeypatch.setattr(settings, "RETRY_BASE_DELAY_SECONDS", 0.1)
    monkeypatch.setattr(settings, "RETRY_MAX_DELAY_SECONDS", 1.0)
    monkeypatch.setattr(settings, "RETRY_JITTER", 0.0)
    assert abs(agents._backoff_delay(1) - 0.1) < 1e-9
    assert abs(agents._backoff_delay(2) - 0.2) < 1e-9
    assert abs(agents._backoff_delay(3) - 0.4) < 1e-9
    assert abs(agents._backoff_delay(10) - 1.0) < 1e-9  # 封顶


def _isolate_ledger(monkeypatch, tmp_path):
    from services import idempotency_service as idem
    monkeypatch.setattr(settings, "CHECKPOINTER_DB", str(tmp_path / "checkpoints.db"))
    monkeypatch.setattr(idem, "_conn", None)
    monkeypatch.setattr(settings, "RETRY_BASE_DELAY_SECONDS", 0.0)
    return idem


def test_executor_idempotent_no_duplicate_side_effect(monkeypatch, tmp_path):
    idem = _isolate_ledger(monkeypatch, tmp_path)
    from workflows import agents

    class OkTool:
        """总是成功的工具替身（替代原 monkeypatch random.random 的做法）。"""

        name = "ok_tool"

        def call(self, payload, timeout=None):
            return "下单成功"

    monkeypatch.setattr(agents, "_TOOL", OkTool())

    state = {
        "thread_id": "t-idem", "trace_id": "tr-idem", "retry_count": 0,
        "idempotency_key": "KEY-1", "request_detail": "下单", "amount": 100.0,
        "department": "技术部", "current_phase": "execute",
    }
    r1 = agents.executor_node(state)
    assert r1["execution_result"] == "success"
    assert idem.count() == 1

    # 同一 key 重放：命中幂等，不再产生新台账记录（无重复副作用）
    r2 = agents.executor_node(state)
    assert r2["execution_result"] == "success"
    assert any("幂等" in x for x in r2["logs"])
    assert idem.count() == 1


def test_executor_crash_window_replay_is_safe(monkeypatch, tmp_path):
    """反例（缺口③）：模拟「执行成功→记账前崩溃」——仅占坑 pending 不更新 success。

    恢复重放时：看到 pending（而非 success）应安全重新执行，最终仅一条 success
    记录；真实命中时不再触发执行。验证「占坑在记账前」消除了崩溃窗口。
    """
    idem = _isolate_ledger(monkeypatch, tmp_path)
    from workflows import agents

    class OkTool:
        name = "ok_tool"

        def call(self, payload, timeout=None):
            return "下单成功"

    monkeypatch.setattr(agents, "_TOOL", OkTool())

    state = {
        "thread_id": "t-crash", "trace_id": "tr-crash", "retry_count": 0,
        "idempotency_key": "CRASH-KEY", "request_detail": "下单", "amount": 100.0,
        "department": "技术部", "current_phase": "execute",
    }
    # 模拟崩溃：执行成功但进程在「记账 success」前崩溃，台账停留在 pending
    idem.claim_pending("CRASH-KEY", "t-crash")
    assert idem.get("CRASH-KEY")["status"] == "pending"

    # 重放：不应误判为命中（status 非 success），应安全重新执行并补记 success
    r = agents.executor_node(state)
    assert r["execution_result"] == "success"
    assert idem.get("CRASH-KEY")["status"] == "success"
    assert idem.count() == 1  # 占坑行不变，不会新增重复记录

    # 真正命中：再次进入直接返回缓存，不再触发执行/记账
    r2 = agents.executor_node(state)
    assert any("幂等" in x for x in r2["logs"])
    assert idem.count() == 1


def test_ledger_migration_from_legacy_schema(monkeypatch, tmp_path):
    """回归（缺口③）：升级前无 status 列的旧 ledger.db 必须能自动迁移并正常使用。"""
    idem = _isolate_ledger(monkeypatch, tmp_path)
    import sqlite3 as _sq
    from pathlib import Path

    legacy = Path(settings.CHECKPOINTER_DB).with_name("ledger.db")
    con = _sq.connect(str(legacy))
    con.execute(
        "CREATE TABLE execution_ledger "
        "(idempotency_key TEXT PRIMARY KEY, thread_id TEXT, result TEXT, payload TEXT, created_at TEXT)"
    )
    con.commit()
    con.close()

    # 触发连接与迁移（旧表无 status 列）
    assert idem.claim_pending("K1", "t1") is True
    idem.update_status("K1", "success", "success", "report")
    assert idem.get("K1")["status"] == "success"
    assert idem.count() == 1
