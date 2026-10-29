"""鉴权依赖（JWT + RBAC）。"""
from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core.security import ROLE_ADMIN, ROLE_APPROVER, decode_jwt
from models.user import User
from services.auth_service import get_by_id

_bearer = HTTPBearer(auto_error=False)


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> User:
    if creds is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少认证令牌", headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = decode_jwt(creds.credentials)
        uid = payload.get("sub")
        user = get_by_id(uid) if uid else None
    except ValueError:
        user = None
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="令牌无效或用户不可用", headers={"WWW-Authenticate": "Bearer"})
    return user


def get_current_user_optional(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> User | None:
    """不强制鉴权：无令牌或令牌无效时返回 None。

    用于冷启动引导等场景（如系统零用户时开放首个管理员创建）。
    """
    if creds is None:
        return None
    try:
        payload = decode_jwt(creds.credentials)
        uid = payload.get("sub")
        user = get_by_id(uid) if uid else None
    except ValueError:
        return None
    if user is None or not user.is_active:
        return None
    return user


def require_roles(*roles: str):
    allowed = set(roles)

    def _dep(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"权限不足，需要角色: {sorted(allowed)}")
        return user

    return _dep


# 常用依赖
AdminUser = require_roles(ROLE_ADMIN)
ApproverUser = require_roles(ROLE_APPROVER, ROLE_ADMIN)
ApplicantUser = require_roles("applicant", ROLE_APPROVER, ROLE_ADMIN)
