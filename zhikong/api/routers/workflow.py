"""工作流路由：提交、状态、待办、列表。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from api.deps import ApplicantUser, get_current_user
from models.user import User
from schemas import ApiResponse, PendingItem, StatusResponse, SubmitRequest
from services.workflow_service import workflow_service

router = APIRouter(prefix="/workflow", tags=["工作流"])


@router.post("/submit", response_model=ApiResponse)
async def submit(req: SubmitRequest, user: User = Depends(ApplicantUser)):
    if not req.request_detail:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="申请详情不能为空")
    res = await workflow_service.submit(
        req.model_dump(), actor=user.username, actor_role=user.role)
    data = StatusResponse(**res["state"]).model_dump()
    data["paused"] = res["paused"]
    data["next"] = res["next"]
    return ApiResponse(data=data, message="已提交" if res["paused"] else "已自动完成")


@router.get("/status/{thread_id}", response_model=ApiResponse)
async def status(thread_id: str, user: User = Depends(get_current_user)):
    st = await workflow_service.status(thread_id)
    if st is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="工作流不存在")
    return ApiResponse(data=StatusResponse(**st).model_dump())


@router.get("/pending", response_model=ApiResponse)
async def pending(user: User = Depends(ApplicantUser)):
    items = await workflow_service.pending()
    return ApiResponse(data={"items": [PendingItem(**i).model_dump() for i in items],
                            "total": len(items)})


@router.get("/list", response_model=ApiResponse)
async def list_workflows(user: User = Depends(get_current_user)):
    # 从审计 WORKFLOW_SUBMIT 反查提交记录（轻量列表）
    from services.audit_service import EventType, query as audit_query
    rows = audit_query(event_type=EventType.WORKFLOW_SUBMIT, size=200)["items"]
    return ApiResponse(data={"items": rows, "total": len(rows)})
