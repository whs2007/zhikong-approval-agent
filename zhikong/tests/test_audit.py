"""审计留痕与持久化（跨重启恢复）测试。"""
from __future__ import annotations

import os

import pytest
from langgraph.types import Command


def _submit_high(client, token):
    r = client.post("/api/v1/workflow/submit",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"request_type": "procurement", "request_detail": "采购服务器",
                          "amount": 500000.0, "department": "技术部"})
    return r.json()["data"]["thread_id"]


def test_audit_trail_present(client, applicant_token, approver_token, admin_token):
    tid = _submit_high(client, applicant_token)
    client.post(f"/api/v1/approval/decide/{tid}",
                headers={"Authorization": f"Bearer {approver_token}"},
                json={"approved": True, "comment": "同意"})
    r = client.get(f"/api/v1/audit/logs?thread_id={tid}",
                   headers={"Authorization": f"Bearer {admin_token}"})
    events = [e["event_type"] for e in r.json()["data"]["items"]]
    for ev in ["WORKFLOW_SUBMIT", "RISK_ANALYZED", "APPROVAL_PAUSED",
               "APPROVAL_DECISION", "WORKFLOW_DONE"]:
        assert ev in events, f"缺少审计事件 {ev}"


def test_audit_export_csv(client, admin_token):
    r = client.get("/api/v1/audit/export?format=csv",
                   headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    assert "event_type" in r.text


def test_persistence_across_restart(client, applicant_token, approver_token):
    """模拟服务重启：用同一 checkpoint 文件新建工作流实例，应能从暂停处恢复。"""
    import sqlite3

    from langgraph.checkpoint.sqlite import SqliteSaver

    from workflows.graph import build_workflow

    tid = _submit_high(client, applicant_token)  # 暂停在审批闸门

    db_path = os.environ["CHECKPOINTER_DB"]
    conn = sqlite3.connect(db_path, check_same_thread=False)
    new_app = build_workflow()  # 复用同一文件（新进程等价物）

    cfg = {"configurable": {"thread_id": tid}}
    snap = new_app.get_state(cfg)
    assert bool(snap.next), "重启后应仍处于暂停状态（持久化生效）"

    # 用 Command.resume 恢复并批准
    result = new_app.invoke(
        Command(resume={"approved": True, "comment": "重启后审批", "actor": "approver", "actor_role": "approver"}),
        cfg)
    assert result["current_phase"] == "done"
    assert result["execution_result"] == "success"
    conn.close()
