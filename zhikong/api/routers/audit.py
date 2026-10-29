"""审计路由：查询与导出（仅管理员）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse

from api.deps import AdminUser
from models.user import User
from schemas import ApiResponse, AuditQueryParams
from services import audit_service

router = APIRouter(prefix="/audit", tags=["审计"])


@router.get("/logs", response_model=ApiResponse)
def logs(q: AuditQueryParams = Depends(), _: User = Depends(AdminUser)):
    res = audit_service.query(
        thread_id=q.thread_id, actor=q.actor, event_type=q.event_type,
        start=q.start, end=q.end, page=q.page, size=q.size)
    return ApiResponse(data=res)


@router.get("/trace/{trace_id}", response_model=ApiResponse)
def trace(trace_id: str, _: User = Depends(AdminUser)):
    return ApiResponse(data=audit_service.trace(trace_id))


@router.get("/export")
def export(
    format: str = "csv",
    thread_id: str | None = None,
    actor: str | None = None,
    event_type: str | None = None,
    start: str | None = None,
    end: str | None = None,
    _: User = Depends(AdminUser),
):
    filters = {"thread_id": thread_id, "actor": actor, "event_type": event_type,
               "start": start, "end": end}
    if format == "json":
        return PlainTextResponse(audit_service.export_json(filters),
                                media_type="application/json")
    return PlainTextResponse(audit_service.export_csv(filters),
                            media_type="text/csv",
                            headers={"Content-Disposition": "attachment; filename=audit.csv"})
