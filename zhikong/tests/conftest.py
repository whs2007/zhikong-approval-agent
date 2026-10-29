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
