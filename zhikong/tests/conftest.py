"""测试夹具：使用临时 DB，避免污染真实数据；并准备认证令牌。"""
from __future__ import annotations

import os
import tempfile

# 必须在导入业务模块前设置环境变量（settings 在 import 时读取）
_TMP = tempfile.mkdtemp(prefix="zhikong_test_")
os.environ["CHECKPOINTER"] = "sqlite"
os.environ["CHECKPOINTER_DB"] = os.path.join(_TMP, "checkpoints.db")
os.environ["AUDIT_DB"] = os.path.join(_TMP, "audit.db")
os.environ["DB_URL"] = f"sqlite:///{os.path.join(_TMP, 'zhikong.db')}"
os.environ["LLM_PROVIDER"] = "rule"
os.environ["EMBEDDING_PROVIDER"] = "local"

import pytest
from fastapi.testclient import TestClient

from api.main import app
from services import knowledge_service as ks


@pytest.fixture(scope="session", autouse=True)
def _build_knowledge():
    ks.build()


@pytest.fixture(scope="session", autouse=True)
def _deterministic_tools():
    """测试环境下让默认工具确定性成功，消除随机失败率导致的 flaky 用例。

    原因（P1-1）：executor 现调用真实工具 SupplierOrderTool，其失败率若在测试下
    保持 0.4，则「依赖最终成功」的集成用例会有约 6% 概率失败。
    工具韧性专项测试（tests/test_tools_resilience.py）显式注入自己的工具替身，
    不受本 fixture 影响。
    """
    from core.config import settings
    settings.TOOL_FAILURE_RATE = 0.0
    settings.TOOL_TIMEOUT_SECONDS = 2.0


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _login(client, username, password):
    r = client.post("/api/v1/auth/login",
                    json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["data"]["access_token"]


@pytest.fixture
def applicant_token(client):
    return _login(client, "applicant01", "applicant123")


@pytest.fixture
def approver_token(client):
    return _login(client, "approver01", "approver123")


@pytest.fixture
def admin_token(client):
    return _login(client, "admin", "admin123")
