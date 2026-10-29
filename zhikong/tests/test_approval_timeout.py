"""P1-7：审批时效（等待时长 / 超时 / 升级）测试。"""
from __future__ import annotations

from datetime import datetime, timedelta

from core.config import settings
from services import audit_service
from services.workflow_service import _aging_info, workflow_service


def test_aging_info_fresh_is_not_overdue():
    now = datetime(2026, 1, 10, 12, 0, 0)
    info = _aging_info("2026-01-10T11:00:00", now)
    assert info["waiting_hours"] == 1.0
    assert info["overdue"] is False
    assert info["escalated"] is False


def test_aging_info_overdue_and_escalated():
    base = datetime(2026, 1, 10, 12, 0, 0)
    paused = (base - timedelta(hours=25)).isoformat(timespec="seconds")
    assert _aging_info(paused, base)["overdue"] is True
    assert _aging_info(paused, base)["escalated"] is False

    old = (base - timedelta(hours=settings.APPROVAL_ESCALATE_HOURS + 1)).isoformat(timespec="seconds")
    assert _aging_info(old, base)["overdue"] is True
    assert _aging_info(old, base)["escalated"] is True


def test_aging_info_tolerates_bad_timestamp():
    """时间缺失/格式异常不应让待审批列表整体报错。"""
    assert _aging_info(None, datetime.now())["waiting_hours"] is None
    assert _aging_info("not-a-time", datetime.now())["waiting_hours"] is None


def test_pending_reports_aging_and_escalation(client, applicant_token):
    """集成：大额申请暂停后，pending 应报告等待时长；超时后标记 overdue 并升级留痕。"""
    r = client.post("/api/v1/workflow/submit",
                    headers={"Authorization": f"Bearer {applicant_token}"},
                    json={"request_type": "procurement", "request_detail": "采购服务器",
                          "amount": 300000.0, "department": "技术部"})
    assert r.json()["data"]["paused"] is True
    tid = r.json()["data"]["thread_id"]

    now = datetime.now()
    # 1) 刚提交：不超时
    fresh = workflow_service._pending_sync(now=now)
    item = next(x for x in fresh if x["thread_id"] == tid)
    assert item["overdue"] is False and item["escalated"] is False
    assert item["waiting_hours"] is not None

    # 2) 超过审批时限：overdue
    late = workflow_service._pending_sync(now=now + timedelta(hours=settings.APPROVAL_TIMEOUT_HOURS + 1))
    late_item = next(x for x in late if x["thread_id"] == tid)
    assert late_item["overdue"] is True
    assert late_item["escalated"] is False

    # 3) 超过升级时限：escalated + 审计留痕
    very_late = workflow_service._pending_sync(
        now=now + timedelta(hours=settings.APPROVAL_ESCALATE_HOURS + 1))
    esc_item = next(x for x in very_late if x["thread_id"] == tid)
    assert esc_item["escalated"] is True

    rows = audit_service.query(event_type=audit_service.EventType.APPROVAL_ESCALATED,
                               thread_id=tid, size=10)["items"]
    assert rows, "升级必须写入审计，否则无法追溯谁拖了多久"
