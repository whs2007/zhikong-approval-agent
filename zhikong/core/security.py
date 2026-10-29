"""安全模块：JWT（HS256，纯标准库实现）与密码哈希（PBKDF2）。

不依赖 python-jose / passlib，降低安装与供应链风险。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import secrets
from typing import Any

from core.config import settings

# ----------------------------- JWT -----------------------------


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def encode_jwt(payload: dict[str, Any], expire_minutes: int | None = None) -> str:
    """签发 HS256 JWT。"""
    exp = int(time.time()) + (expire_minutes or settings.JWT_EXPIRE_MINUTES) * 60
    body = {**payload, "exp": exp, "iat": int(time.time())}
    header = {"alg": settings.JWT_ALG, "typ": "JWT"}
    seg1 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    seg2 = _b64url_encode(json.dumps(body, separators=(",", ":")).encode())
    signing_input = f"{seg1}.{seg2}".encode("ascii")
    sig = hmac.new(settings.SECRET_KEY.encode(), signing_input, hashlib.sha256).digest()
    seg3 = _b64url_encode(sig)
    return f"{seg1}.{seg2}.{seg3}"


def decode_jwt(token: str) -> dict[str, Any]:
    """校验并返回 JWT payload；失败抛 ValueError。"""
    try:
        seg1, seg2, seg3 = token.split(".")
    except ValueError:
        raise ValueError("invalid token format")
    signing_input = f"{seg1}.{seg2}".encode("ascii")
    expected = hmac.new(settings.SECRET_KEY.encode(), signing_input, hashlib.sha256).digest()
    got = _b64url_decode(seg3)
    if not hmac.compare_digest(expected, got):
        raise ValueError("signature verification failed")
    payload = json.loads(_b64url_decode(seg2))
    if "exp" in payload and payload["exp"] < int(time.time()):
        raise ValueError("token expired")
    return payload


# --------------------------- 密码哈希 ---------------------------


def hash_password(password: str) -> str:
    """PBKDF2-HMAC-SHA256 + 随机盐，返回 'salt$iterations$hash'。"""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return f"{salt.hex()}$100000${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, iters, hash_hex = stored.split("$")
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters)
    )
    return hmac.compare_digest(dk.hex(), hash_hex)


# --------------------------- 凭据强度策略 ---------------------------

MIN_USERNAME_LENGTH = 3
MIN_PASSWORD_LENGTH = 8
# 常见弱口令字典：命中即拒绝，杜绝教学/演示弱口令被直接用于生产账号
_WEAK_PASSWORDS = {
    "admin", "admin123", "password", "password123", "123456", "12345678",
    "root", "root123", "qwerty", "letmein", "changeme", "secret",
    "zhikong", "zhikong123", "administrator", "test", "test123",
}


class CredentialError(ValueError):
    """凭据不满足强度策略；HTTP 层应将其转为 400。"""


def validate_username(username: str) -> str:
    """校验用户名合法性，返回清洗后的用户名；非法抛 CredentialError。"""
    if not isinstance(username, str) or not username.strip():
        raise CredentialError("用户名不能为空")
    username = username.strip()
    if len(username) < MIN_USERNAME_LENGTH:
        raise CredentialError(f"用户名长度至少需 {MIN_USERNAME_LENGTH} 位")
    if not username.replace("_", "").isalnum():
        raise CredentialError("用户名仅允许字母、数字与下划线")
    return username


def validate_password_strength(password: str) -> str:
    """校验密码强度，返回原口令；不满足策略抛 CredentialError。"""
    if not isinstance(password, str) or not password:
        raise CredentialError("密码不能为空")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise CredentialError(f"密码长度至少需 {MIN_PASSWORD_LENGTH} 位")
    if password.lower() in _WEAK_PASSWORDS:
        raise CredentialError("密码过于常见，请使用更强的口令")
    # 拒绝纯数字 / 纯字母（单类字符熵过低）
    if password.isdigit() or password.isalpha():
        raise CredentialError("密码需同时包含字母与数字（或符号）")
    # 拒绝字符多样性过低（如 'aaaa1111'）
    if len(set(password)) < 4:
        raise CredentialError("密码字符多样性不足，请勿重复使用少数几种字符")
    return password


# --------------------------- 角色常量 ---------------------------

ROLE_APPLICANT = "applicant"
ROLE_APPROVER = "approver"
ROLE_ADMIN = "admin"
ALL_ROLES = [ROLE_APPLICANT, ROLE_APPROVER, ROLE_ADMIN]
