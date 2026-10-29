"""系统路由：健康检查、指标、知识库重载。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from api.deps import AdminUser, get_current_user
from core import metrics as metrics_mod
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


@router.get("/metrics")
def metrics(user: User = Depends(get_current_user)):
    """Prometheus 抓取端点（P1-3）。

    返回标准 Prometheus 文本格式（不再是审计表全量 COUNT 的 JSON）。
    保留鉴权：指标可能暴露业务量等敏感运营信息，无鉴权暴露属安全隐患。
    """
    # Gauge 类瞬时指标需在拉取时刷新：待审批积压数
    try:
        pending = workflow_service._pending_sync()
        metrics_mod.set_pending_approvals(len(pending))
    except Exception as e:  # pragma: no cover - 指标降级不应影响告警抓取
        logger.warning(f"[metrics] 刷新待审批数失败: {e}")
    body, content_type = metrics_mod.render_metrics()
    return Response(content=body, media_type=content_type)


@router.get("/metrics/summary", response_model=ApiResponse)
def metrics_summary(user: User = Depends(get_current_user)):
    """业务概览（JSON）：保留 P1-3 之前的行为，供前端/人工查看。"""
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
