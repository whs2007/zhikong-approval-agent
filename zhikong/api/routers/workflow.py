"""工作流路由：提交、状态、待办、列表。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status
from fastapi.responses import StreamingResponse

from api.deps import ApplicantUser, ApplicantUserStream, get_current_user
from models.user import User
from schemas import ApiResponse, PendingItem, StatusResponse, SubmitRequest
from services.workflow_service import workflow_service

router = APIRouter(prefix="/workflow", tags=["工作流"])


@router.post("/submit", response_model=ApiResponse)
async def submit(req: SubmitRequest, user: User = Depends(ApplicantUser)):
    if not req.request_detail:
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail="申请详情不能为空")
    res = await workflow_service.submit(
        req.model_dump(), actor=user.username, actor_role=user.role)
    data = StatusResponse(**res["state"]).model_dump()
    data["paused"] = res["paused"]
    data["next"] = res["next"]
    return ApiResponse(data=data, message="已提交" if res["paused"] else "已自动完成")


@router.post("/submit/stream")
def submit_stream(req: SubmitRequest, user: User = Depends(ApplicantUserStream)):
    """提交工作流并以 SSE 推送节点级执行进度。

    与 POST /submit 的差别只在传输方式：状态机、审计、指标、幂等规则完全一致
    （同一套 `_submit_stream_sync`→`app.stream` 与 `submit`→`app.invoke` 走同一图）。

    为什么是 POST 而非 EventSource 的 GET：提交要带请求体。浏览器原生 EventSource
    只支持 GET，所以前端用 `fetch` 读流（或先提交再订阅展示端点）；鉴权同时支持
    `Authorization` 头与 `?token=` 兜底（见 deps.get_current_user_stream）。
    """
    if not req.request_detail:
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail="申请详情不能为空")
    events = workflow_service.submit_stream(
        req.model_dump(), actor=user.username, actor_role=user.role)
    return StreamingResponse(
        events,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 关键：关掉反向代理缓冲，否则 nginx 会把事件攒成一批，
            # 前端看到的仍是"最后一次性到达"，流式就白做了。
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/status/{thread_id}", response_model=ApiResponse)
async def status(thread_id: str, user: User = Depends(get_current_user)):
    st = await workflow_service.status(thread_id)
    if st is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="工作流不存在")
    return ApiResponse(data=StatusResponse(**st).model_dump())


@router.get("/pending", response_model=ApiResponse)
async def pending(user: User = Depends(ApplicantUser)):
    items = await workflow_service.pending()
    return ApiResponse(data={"items": [PendingItem(**i).model_dump() for i in items],
                            "total": len(items)})


@router.get("/list", response_model=ApiResponse)
async def list_workflows(user: User = Depends(get_current_user)):
    # 从审计 WORKFLOW_SUBMIT 反查提交记录（轻量列表）
    from services.audit_service import EventType
    from services.audit_service import query as audit_query
    rows = audit_query(event_type=EventType.WORKFLOW_SUBMIT, size=200)["items"]
    return ApiResponse(data={"items": rows, "total": len(rows)})
