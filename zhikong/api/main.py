"""FastAPI 应用装配。"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from api.routers import approval, audit, auth, system, workflow
from core.config import BASE_DIR, settings
from core.logging import logger
from core.tracing import TraceIdMiddleware
from services import knowledge_service as ks

FRONTEND = BASE_DIR / "frontend" / "index.html"


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
    yield


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
