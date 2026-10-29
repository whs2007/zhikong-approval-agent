"""接口与鉴权测试。"""
from __future__ import annotations

from tests.conftest import DEMO_PASSWORDS


def test_login_success(client):
    r = client.post("/api/v1/auth/login",
                    json={"username": "admin", "password": DEMO_PASSWORDS["admin"]})
    assert r.status_code == 200
    assert r.json()["data"]["role"] == "admin"


def test_login_fail(client):
    r = client.post("/api/v1/auth/login",
                    json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401


def test_no_token_rejected(client):
    r = client.get("/api/v1/workflow/list")
    assert r.status_code == 401


def test_applicant_cannot_approve(client, applicant_token):
    # 申请人提交一个高风险申请
    r = client.post("/api/v1/workflow/submit",
                    headers={"Authorization": f"Bearer {applicant_token}"},
                    json={"request_type": "procurement", "request_detail": "x",
                          "amount": 300000.0, "department": "技术部"})
    assert r.status_code == 200
    tid = r.json()["data"]["thread_id"]
    # 申请人尝试审批 -> 403
    r2 = client.post(f"/api/v1/approval/decide/{tid}",
                     headers={"Authorization": f"Bearer {applicant_token}"},
                     json={"approved": True, "comment": "x"})
    assert r2.status_code == 403


def test_audit_requires_admin(client, applicant_token, admin_token):
    r = client.get("/api/v1/audit/logs",
                   headers={"Authorization": f"Bearer {applicant_token}"})
    assert r.status_code == 403
    r2 = client.get("/api/v1/audit/logs",
                    headers={"Authorization": f"Bearer {admin_token}"})
    assert r2.status_code == 200
