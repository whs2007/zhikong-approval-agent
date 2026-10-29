"""系统路由：健康检查、指标、知识库重载。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from api.deps import AdminUser, get_current_user
from core.config import settings
from core.logging import logger
from models.user import User
from schemas import ApiResponse
from services import knowledge_service as ks
from services.audit_service import EventType, append, query as audit_query
from services.workflow_service import workflow_service

router = APIRouter(tags=["系统"])


@router.get("/health", response_model=ApiResponse)
def health():
    # 简易健康检查：检查审计库可写 + Checkpointer 可用
    try:
        audit_query(size=1)
        checks = {"audit_db": True, "checkpointer": True,
                  "knowledge_chunks": ks.chunk_count()}
    except Exception as e:  # pragma: no cover
        checks = {"audit_db": False, "error": str(e)}
    return ApiResponse(data={"status": "ok" if checks.get("audit_db") else "degraded",
                            "checks": checks, "env": settings.ENV})


@router.get("/metrics", response_model=ApiResponse)
def metrics(user: User = Depends(get_current_user)):
    rows = audit_query(size=100000)["items"]
    total = len(rows)
    success = sum(1 for r in rows if r["event_type"] == EventType.WORKFLOW_DONE
                  and "成功" in (r["detail"] or ""))
    failed = sum(1 for r in rows if r["event_type"] == EventType.WORKFLOW_DONE
                 and "失败" in (r["detail"] or ""))
    pending = sum(1 for r in rows if r["event_type"] == EventType.APPROVAL_PAUSED)
    return ApiResponse(data={
        "total_events": total, "workflow_done_success": success,
        "workflow_done_failed": failed, "approval_paused": pending,
        "knowledge_chunks": ks.chunk_count()})


@router.post("/knowledge/reload", response_model=ApiResponse)
def reload_knowledge(user: User = Depends(AdminUser)):
    count = ks.build()
    append(EventType.KNOWLEDGE_RELOAD, actor=user.username, actor_role=user.role,
           detail={"chunks": count})
    logger.info(f"[knowledge] 重载完成: {count} 块")
    return ApiResponse(data={"chunks": count}, message="知识库已重载")
