"""SSE 流式提交（POST /workflow/submit/stream）的协议与语义测试。

测什么、为什么：

1. **事件序列**：必须 start → node... → done。
   只测"返回 200"是假门禁——流式最容易的 bug 是"看起来是流，其实是最后一个
   chunk 一次性吐出"。这里断言事件数量与顺序，能抓到"退化成非流式"的回归。
2. **中断语义**：大额进审批时 done.paused=true 且 next 含 approval_gate；
   小额自动放行时 paused=false。这是简历里 HITL 的核心，必须可验证。
3. **鉴权双通道**：Bearer 头可用；`?token=` 兜底可用；两者都无 → 401。
   如果只测头，`?token=` 那条分支出问题不会被发现（前端 EventSource 就靠它）。
4. **帧格式**：每帧必须有 id/event/data，且以空行结尾，否则浏览器解析不到。
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from api.main import app

STREAM_URL = "/api/v1/workflow/submit/stream"


def _parse_sse(text: str) -> list[dict]:
    """把 SSE 原始文本解析成 [{event, data, id}]，顺带校验帧结构。"""
    events: list[dict] = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        evt: dict = {}
        for line in block.splitlines():
            if line.startswith(":"):          # 注释/心跳
                continue
            field, _, value = line.partition(": ")
            if field == "data":
                evt["data"] = json.loads(value)
            elif field in ("event", "id"):
                evt[field] = value
        assert "event" in evt, f"帧缺少 event 字段: {block!r}"
        assert "data" in evt, f"帧缺少 data 字段: {block!r}"
        events.append(evt)
    return events


def _submit_stream(client: TestClient, headers: dict | None, params: dict | None,
                   body: dict) -> tuple[int, str]:
    with client.stream("POST", STREAM_URL, headers=headers or {},
                       params=params or {}, json=body) as resp:
        return resp.status_code, resp.read().decode()


def _body(amount: float) -> dict:
    return {
        "request_type": "procurement",
        "request_detail": f"采购测试物料，金额 {amount}",
        "amount": amount,
        "department": "研发部",
    }


def test_stream_requires_auth(client):
    status, _ = _submit_stream(client, None, None, _body(1000))
    assert status == 401


def test_stream_high_amount_pauses_at_gate(client, applicant_token):
    """大额 → 进审批闸门：事件里能看到 analyst，done 里 paused=true。"""
    status, text = _submit_stream(
        client, {"Authorization": f"Bearer {applicant_token}"}, None, _body(80000))
    assert status == 200
    events = _parse_sse(text)

    names = [e["event"] for e in events]
    assert names[0] == "start", f"首帧必须是 start，实际 {names}"
    assert names[-1] == "done", f"末帧必须是 done，实际 {names}"
    assert "error" not in names

    nodes = [e["data"]["node"] for e in events if e["event"] == "node"]
    assert "analyst" in nodes, f"未推送 analyst 节点进度: {nodes}"
    # 中断发生在 approval_gate，故 executor 此时不应出现
    assert "executor" not in nodes, f"审批前不应执行 executor: {nodes}"

    done = events[-1]["data"]
    assert done["paused"] is True
    assert done["final"] == "pending_approval"
    assert "approval_gate" in done["next"]
    assert done["state"]["risk_level"] in ("medium", "high")


def test_stream_low_amount_runs_to_completion(client, applicant_token):
    """小额 → 自动放行，应看到 executor 与 success 终态。"""
    status, text = _submit_stream(
        client, {"Authorization": f"Bearer {applicant_token}"}, None, _body(500))
    assert status == 200
    events = _parse_sse(text)
    nodes = [e["data"]["node"] for e in events if e["event"] == "node"]
    assert "analyst" in nodes and "executor" in nodes, f"节点序列异常: {nodes}"

    done = events[-1]["data"]
    assert done["paused"] is False
    assert done["final"] in ("success", "failed")   # 工具失败率在测试中被置 0


def test_stream_token_query_fallback(client, applicant_token):
    """EventSource 场景：无 Authorization 头，仅 ?token= 也应放行。"""
    status, text = _submit_stream(client, None, {"token": applicant_token}, _body(500))
    assert status == 200
    assert _parse_sse(text)[0]["event"] == "start"


def test_stream_rejects_bad_query_token(client):
    status, _ = _submit_stream(client, None, {"token": "not-a-jwt"}, _body(500))
    assert status == 401


def test_stream_rejects_empty_detail(client, applicant_token):
    status, _ = _submit_stream(
        client, {"Authorization": f"Bearer {applicant_token}"}, None,
        {"request_type": "procurement", "request_detail": "", "amount": 100})
    assert status == 400


def test_event_ids_are_monotonic(client, applicant_token):
    """id 递增，便于前端断线后按 Last-Event-ID 续传。"""
    _, text = _submit_stream(
        client, {"Authorization": f"Bearer {applicant_token}"}, None, _body(500))
    ids = [int(e["id"]) for e in _parse_sse(text)]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


def test_stream_writes_workflow_submit_audit(client, applicant_token):
    """流式提交必须与普通提交一样留痕（否则审计出现盲区）。"""
    _, text = _submit_stream(
        client, {"Authorization": f"Bearer {applicant_token}"}, None, _body(999))
    done = _parse_sse(text)[-1]["data"]
    thread_id = done["thread_id"]

    from services.audit_service import EventType
    from services.audit_service import query as audit_query
    rows = audit_query(event_type=EventType.WORKFLOW_SUBMIT, size=200)["items"]
    assert any(r["thread_id"] == thread_id for r in rows), "流式提交未写 WORKFLOW_SUBMIT 审计"


def test_two_streams_do_not_share_thread(client, applicant_token):
    """两个并发/连续流式提交应生成不同 thread_id（防止配置串用）。"""
    _, t1 = _submit_stream(
        client, {"Authorization": f"Bearer {applicant_token}"}, None, _body(500))
    _, t2 = _submit_stream(
        client, {"Authorization": f"Bearer {applicant_token}"}, None, _body(500))
    assert _parse_sse(t1)[-1]["data"]["thread_id"] != _parse_sse(t2)[-1]["data"]["thread_id"]
