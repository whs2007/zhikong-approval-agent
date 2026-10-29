"""审批路由：人工审批决策（Human-in-the-Loop 恢复）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from api.deps import ApproverUser
from models.user import User
from schemas import ApiResponse, DecideRequest, StatusResponse
from services.workflow_service import workflow_service

router = APIRouter(prefix="/approval", tags=["审批"])


@router.post("/decide/{thread_id}", response_model=ApiResponse)
async def decide(thread_id: str, req: DecideRequest,
                user: User = Depends(ApproverUser)):
    st = await workflow_service.status(thread_id)
    if st is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="工作流不存在")
    if not st.get("paused"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                          detail="该工作流当前不在待审批状态")
    result = await workflow_service.decide(
        thread_id, req.approved, req.comment,
        actor=user.username, actor_role=user.role)
    return ApiResponse(data=StatusResponse(**result).model_dump(),
                      message="已批准并恢复执行" if req.approved else "已驳回")
