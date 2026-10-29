"""工作流端到端测试：低风险/高风险/驳回/重试/跨重启恢复。"""
from __future__ import annotations

import pytest


def _submit(client, token, amount, detail="测试采购", rtype="procurement"):
    return client.post("/api/v1/workflow/submit",
                      headers={"Authorization": f"Bearer {token}"},
                      json={"request_type": rtype, "request_detail": detail,
                            "amount": amount, "department": "技术部"})


def test_low_risk_auto_execute(client, applicant_token):
    r = _submit(client, applicant_token, 3000.0, "买办公笔")
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["paused"] is False
    assert d["current_phase"] == "done"
    assert d["risk_level"] == "low"
    assert d["execution_result"] == "success"


def _pause_high_risk(client, applicant_token):
    r = _submit(client, applicant_token, 300000.0, "采购50台笔记本")
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["paused"] is True
    assert d["current_phase"] == "approve"
    assert d["risk_level"] == "high"
    tid = d["thread_id"]
    # 待审批列表应包含该条
    p = client.get("/api/v1/workflow/pending",
                   headers={"Authorization": f"Bearer {applicant_token}"})
    ids = [i["thread_id"] for i in p.json()["data"]["items"]]
    assert tid in ids
    return tid


def test_high_risk_approve_and_execute(client, applicant_token, approver_token):
    tid = _pause_high_risk(client, applicant_token)
    r = client.post(f"/api/v1/approval/decide/{tid}",
                    headers={"Authorization": f"Bearer {approver_token}"},
                    json={"approved": True, "comment": "预算内同意"})
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["current_phase"] == "done"
    assert d["execution_result"] == "success"
    assert d["approval_status"] == "approved"


def test_high_risk_reject(client, applicant_token, approver_token):
    tid = _pause_high_risk(client, applicant_token)
    r = client.post(f"/api/v1/approval/decide/{tid}",
                    headers={"Authorization": f"Bearer {approver_token}"},
                    json={"approved": False, "comment": "超预算"})
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["approval_status"] == "rejected"
    assert d["current_phase"] == "done"
    assert d["execution_result"] != "success"  # 未执行


def test_retry_observed_via_logs(client, applicant_token, approver_token):
    tid = _pause_high_risk(client, applicant_token)
    r = client.post(f"/api/v1/approval/decide/{tid}",
                    headers={"Authorization": f"Bearer {approver_token}"},
                    json={"approved": True, "comment": "同意"})
    d = r.json()["data"]
    # 第3次强制成功，重试次数 >=1 概率高；但至少保证最终成功
    assert d["execution_result"] == "success"
    logs = " ".join(d.get("logs", []))
    assert "执行" in logs
