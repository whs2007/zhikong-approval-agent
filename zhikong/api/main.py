"""FastAPI 应用装配。"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from api.routers import approval, audit, auth, system, workflow
from core import metrics as metrics_mod
from core.config import BASE_DIR, settings
from core.logging import logger
from core.metrics import MetricsMiddleware
from core.tracing import TraceIdMiddleware
from services import knowledge_service as ks
from services.workflow_service import workflow_service

FRONTEND = BASE_DIR / "frontend" / "index.html"

# 瞬时指标（待审批数 / 向量索引规模）的后台刷新间隔。
# 10s 足够：这两个都是慢变量，且远小于 Prometheus 的 15s 抓取间隔。
_GAUGE_REFRESH_SECONDS = 10.0


async def _refresh_gauges_forever() -> None:
    """后台刷新 Gauge，**不占用抓取请求**。

    为什么不能放在 /api/v1/metrics 里算（原来的做法）：
      该端点是 `def`（同步），FastAPI 会把它丢进默认线程池。业务高峰时线程池
      被提交请求占满，抓取要排队；实测压测期间一次抓取耗时正好卡在
      scrape_timeout=10s，Prometheus 判定 up=0，**服务最需要监控时指标先断流**，
      延迟告警因此永远不会触发（见 docs/22）。
      改成后台任务后，抓取只读内存里的 Gauge，耗时与业务负载解耦。
    """
    while True:
        try:
            items = await workflow_service.pending()
            metrics_mod.set_pending_approvals(len(items))
            metrics_mod.set_vector_backend(ks.backend_name(), ks.chunk_count())
        except Exception as e:  # pragma: no cover - 指标刷新失败不应影响主流程
            logger.warning(f"[metrics] 后台刷新指标失败: {e}")
        await asyncio.sleep(_GAUGE_REFRESH_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 生产环境启动合规自检：拒绝使用默认/弱密钥启动，避免 JWT 可被伪造
    try:
        settings.validate_for_production()
    except RuntimeError as e:  # pragma: no cover - 生产配置错误需立即暴露
        logger.error(f"[startup] 生产环境合规自检未通过，拒绝启动: {e}")
        raise
    # 启动时构建知识库（RAG 向量化）
    try:
        cnt = ks.build()
        logger.info(f"[startup] 知识库构建完成: {cnt} 块")
    except Exception as e:  # pragma: no cover
        logger.warning(f"[startup] 知识库构建失败: {e}")

    refresher = asyncio.create_task(_refresh_gauges_forever(), name="metrics-gauge-refresher")
    try:
        yield
    finally:
        refresher.cancel()
        with suppress(asyncio.CancelledError):
            await refresher


app = FastAPI(
    title="智控 · 企业级智能工作流平台",
    version="1.0.0",
    description="基于 LangGraph 的多 Agent 协作 + RAG 决策 + Human-in-the-Loop 审批 + 图级别重试 + 审计",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# P1-3：HTTP 请求计数与耗时（放在 TraceId 之内侧，两者都在 CORS 外层）
app.add_middleware(MetricsMiddleware)
# P1-2：后添加=更外层，确保覆盖包括 CORS 在内的全部请求路径
app.add_middleware(TraceIdMiddleware)

app.include_router(auth.router, prefix="/api/v1")
app.include_router(workflow.router, prefix="/api/v1")
app.include_router(approval.router, prefix="/api/v1")
app.include_router(audit.router, prefix="/api/v1")
app.include_router(system.router, prefix="/api/v1")


@app.exception_handler(Exception)
async def global_exc(request: Request, exc: Exception):
    logger.error(f"[api] 未处理异常: {exc}")
    return JSONResponse(status_code=500, content={"code": 50001, "message": "服务器内部错误"})


@app.get("/", include_in_schema=False)
def index():
    if FRONTEND.exists():
        return FileResponse(str(FRONTEND))
    return JSONResponse({"message": "智控平台 API 运行中", "docs": "/docs"})


# 托管前端静态资源（如后续拆分 js/css）
assets = BASE_DIR / "frontend"
if assets.exists():
    app.mount("/static", StaticFiles(directory=str(assets), html=False), name="static")
