"""认证路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from api.deps import get_current_user, get_current_user_optional
from core.logging import logger
from core.security import ROLE_ADMIN, CredentialError, encode_jwt
from models.user import User
from schemas import ApiResponse, LoginRequest, TokenResponse, UserInfo
from services.audit_service import EventType, append
from services.auth_service import (
    authenticate,
    create_first_admin_if_absent,
    create_user,
    get_user_count,
)

router = APIRouter(prefix="/auth", tags=["认证"])


def _create_admin(username: str, password: str, display_name: str) -> User:
    """创建管理员，并把凭据强度错误（CredentialError）转为 400。"""
    try:
        return create_user(username, password, ROLE_ADMIN, display_name)
    except CredentialError as e:
        # from None：不把内部异常链带到 HTTP 层，仅暴露校验原因
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from None


@router.post("/login", response_model=ApiResponse)
def login(req: LoginRequest):
    user = authenticate(req.username, req.password)
    if not user:
        append(EventType.AUTH_FAIL, actor=req.username, detail={"reason": "密码错误"})
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    token = encode_jwt({"sub": user.id, "role": user.role, "username": user.username})
    append(EventType.AUTH_LOGIN, actor=user.username, actor_role=user.role)
    logger.info(f"[auth] 登录成功: {user.username}")
    return ApiResponse(data=TokenResponse(
        access_token=token, role=user.role,
        username=user.username, display_name=user.display_name).model_dump())


@router.get("/me", response_model=ApiResponse)
def me(user: User = Depends(get_current_user)):
    return ApiResponse(data=UserInfo(**user.to_public()).model_dump())


@router.post("/init-admin", response_model=ApiResponse)
def init_admin(
    username: str, password: str, display_name: str = "管理员",
    user: User | None = Depends(get_current_user_optional),
):
    """引导/创建管理员。

    - 系统**无任何用户**时（生产冷启动，默认演示账号未种子）：开放调用，
      用于创建首个管理员，避免全新部署无法引导而锁死。
    - 已存在用户后：必须携带现有管理员令牌，否则 403，防止公开注册管理员。
    - 用户名/密码强制强度策略（见 core.security），弱口令直接 400 拒绝。
    """
    if get_user_count() == 0:
        # 原子冷启动：count+insert 在同一临界区，并发下也只创建唯一首个管理员
        try:
            created, outcome = create_first_admin_if_absent(username, password, display_name)
        except CredentialError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from None
        if created is None:
            # 竞速落败时可能拿不到新建对象；显式 500 优于对 None 取属性
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                                detail="管理员创建失败，请重试")
        if outcome == "created":
            append(EventType.ADMIN_BOOTSTRAP, actor=username,
                   detail={"via": "init-admin-bootstrap"})
            logger.info(f"[auth] 首次冷启动引导管理员: {username}")
            return ApiResponse(data=UserInfo(**created.to_public()).model_dump(),
                               message="首个管理员已创建（冷启动引导）")
        # 并发场景下已被其他请求抢先创建，返回现有管理员而非报错
        append(EventType.ADMIN_BOOTSTRAP, actor=username,
               detail={"via": "init-admin-race-lost", "outcome": outcome})
        logger.info(f"[auth] 冷启动引导竞速落败，返回现有管理员: {username}")
        return ApiResponse(data=UserInfo(**created.to_public()).model_dump(),
                           message="管理员已存在（并发引导，已返回现有管理员）")
    if user is None or user.role != ROLE_ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    created = _create_admin(username, password, display_name)
    append(EventType.ADMIN_CREATED, actor=user.username, detail={"new_admin": username})
    logger.info(f"[auth] 管理员 {user.username} 创建了新管理员: {username}")
    return ApiResponse(data=UserInfo(**created.to_public()).model_dump(), message="管理员已创建")
