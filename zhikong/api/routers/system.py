"""系统路由：健康检查、指标、知识库重载。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from api.deps import AdminUser, get_scrape_access
from core import metrics as metrics_mod
from core.config import settings
from core.logging import logger
from models.user import User
from schemas import ApiResponse
from services import knowledge_service as ks
from services.audit_service import EventType, append
from services.audit_service import query as audit_query

router = APIRouter(tags=["系统"])


@router.get("/health", response_model=ApiResponse)
def health():
    # 简易健康检查：检查审计库可写 + Checkpointer 可用
    try:
        audit_query(size=1)
        checks = {"audit_db": True, "checkpointer": True,
                  "knowledge_chunks": ks.chunk_count(),
                  # 暴露"实际生效"的向量后端：auto 模式下会按依赖降级，
                  # 不暴露就无法发现"以为 FAISS、实际 numpy"的静默不一致。
                  "vector_backend": ks.backend_name()}
    except Exception as e:  # pragma: no cover
        checks = {"audit_db": False, "error": str(e)}
    return ApiResponse(data={"status": "ok" if checks.get("audit_db") else "degraded",
                            "checks": checks, "env": settings.ENV})


@router.get("/metrics")
def metrics(_: None = Depends(get_scrape_access)):
    """Prometheus 抓取端点（P1-3 + T1 可观测性闭环）。

    返回标准 Prometheus 文本格式（不再是审计表全量 COUNT 的 JSON）。
    鉴权保留：接受有效 JWT 或静态 METRICS_TOKEN（二选一），避免指标被匿名暴露。

    这里**只做内存渲染**，不查库：Gauge 由 `api.main._refresh_gauges_forever`
    在后台刷新。原因见该函数注释——把查询放在抓取路径上，高负载时会因线程池
    排队而超时，导致"过载时监控先断流、告警永远不触发"。
    """
    body, content_type = metrics_mod.render_metrics()
    return Response(content=body, media_type=content_type)


@router.get("/metrics/summary", response_model=ApiResponse)
def metrics_summary(_: None = Depends(get_scrape_access)):
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
