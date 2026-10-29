"""数据契约（Pydantic 模型）。"""
from __future__ import annotations

from pydantic import BaseModel


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    username: str
    display_name: str


class UserInfo(BaseModel):
    id: str
    username: str
    role: str
    display_name: str
    is_active: bool = True


class SubmitRequest(BaseModel):
    request_type: str = "procurement"
    request_detail: str
    amount: float
    department: str = ""
    thread_id: str | None = None
    idempotency_key: str | None = None


class StatusResponse(BaseModel):
    request_id: str | None = None
    thread_id: str | None = None
    request_type: str | None = None
    request_detail: str | None = None
    amount: float | None = None
    department: str | None = None
    current_phase: str | None = None
    risk_level: str | None = None
    approval_status: str | None = None
    retry_count: int | None = None
    error: str | None = None
    knowledge_context: str | None = None
    analysis_report: str | None = None
    execution_result: str | None = None
    final_report: str | None = None
    required_approvers: list | None = None
    logs: list | None = None
    trace_id: str | None = None
    idempotency_key: str | None = None
    paused: bool = False
    # P1-7：审批时效（基于进入审批闸门的时间计算）
    waiting_hours: float | None = None
    overdue: bool = False            # 超过 APPROVAL_TIMEOUT_HOURS
    escalated: bool = False          # 超过 APPROVAL_ESCALATE_HOURS（需升级处理）


class PendingItem(StatusResponse):
    pass


class DecideRequest(BaseModel):
    approved: bool
    comment: str = ""


class ApiResponse(BaseModel):
    code: int = 0
    message: str = "ok"
    data: object | None = None
    trace_id: str | None = None


class AuditQueryParams(BaseModel):
    thread_id: str | None = None
    actor: str | None = None
    event_type: str | None = None
    start: str | None = None
    end: str | None = None
    page: int = 1
    size: int = 50
