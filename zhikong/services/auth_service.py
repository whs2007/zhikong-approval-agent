"""认证与用户服务（RBAC）。

- 用户存储于独立 SQLite（zhikong.db），PBKDF2 密码哈希（见 core.security）。
- 首次启动创建默认管理员（admin/admin123），并预置演示审批人/申请人。
- 认证接口由 api/deps.py 依赖注入调用。
"""
from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime

from core.config import settings
from core.logging import logger
from core.security import (
    ALL_ROLES,
    CredentialError,
    ROLE_ADMIN,
    hash_password,
    validate_password_strength,
    validate_username,
    verify_password,
)
from models.user import USER_TABLE_DDL, User

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _conn_get() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(settings.DB_URL.replace("sqlite:///", ""), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(USER_TABLE_DDL)
        _seed()
    return _conn


def _seed() -> None:
    cur = _conn_get()
    cnt = cur.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if cnt > 0:
        return
    # 生产环境不预置任何弱口令演示账号；首个管理员须通过 /auth/init-admin 冷启动引导
    if settings.is_production():
        logger.info("[auth] 生产环境：跳过默认账号种子，请通过 /api/v1/auth/init-admin 引导首个管理员")
        return
    defaults = [
        ("admin", "admin123", "admin", "系统管理员"),
        ("approver01", "approver123", "approver", "审批人-张经理"),
        ("applicant01", "applicant123", "applicant", "申请人-小李"),
    ]
    now = datetime.now().isoformat(timespec="seconds")
    for username, pwd, role, name in defaults:
        cur.execute(
            "INSERT OR IGNORE INTO users (id,username,hashed_password,role,display_name,created_at,is_active) "
            "VALUES (?,?,?,?,?,?,1)",
            (uuid.uuid4().hex, username, hash_password(pwd), role, name, now),
        )
    cur.commit()
    logger.info("[auth] seeded default users (admin/approver01/applicant01)")


def create_user(username: str, password: str, role: str, display_name: str) -> User:
    # 弱口令治理：所有外部创建账号路径（init-admin、未来注册）强制强度策略
    username = validate_username(username)
    password = validate_password_strength(password)
    if role not in ALL_ROLES:
        raise ValueError(f"非法角色: {role}")
    with _lock:
        cur = _conn_get()
        uid = uuid.uuid4().hex
        now = datetime.now().isoformat(timespec="seconds")
        cur.execute(
            "INSERT INTO users (id,username,hashed_password,role,display_name,created_at,is_active) "
            "VALUES (?,?,?,?,?,?,1)",
            (uid, username, hash_password(password), role, display_name, now),
        )
        cur.commit()
    return get_by_username(username)


def create_first_admin_if_absent(
    username: str, password: str, display_name: str
) -> tuple[User | None, str]:
    """原子冷启动引导：无用户时创建首个管理员，否则返回已存在的首个管理员。

    解决 init-admin 并发原子性问题——原实现先 get_user_count() 再 create_user，
    二者不在同一临界区，并发下可能创建多个管理员或抛 500。
    本函数在进程锁 `_lock` 内完成「count + insert」不可分割的临界区；
    跨进程并发由 username UNIQUE 约束兜底（捕获 IntegrityError 回退读取）。
    返回 (user, outcome)，outcome ∈ {'created', 'already_existed'}。
    """
    username = validate_username(username)
    password = validate_password_strength(password)
    with _lock:
        cur = _conn_get()
        cnt = cur.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if cnt > 0:
            row = cur.execute(
                "SELECT * FROM users ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
            return (_row_to_user(row) if row else None, "already_existed")
        try:
            cur.execute(
                "INSERT INTO users (id,username,hashed_password,role,display_name,created_at,is_active) "
                "VALUES (?,?,?,?,?,?,1)",
                (uuid.uuid4().hex, username, hash_password(password), ROLE_ADMIN,
                 display_name, datetime.now().isoformat(timespec="seconds")),
            )
            cur.commit()
        except sqlite3.IntegrityError:
            # 并发下被他人抢先创建（username 唯一约束），回退读取现有管理员
            cur.rollback()
            row = cur.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone() or cur.execute(
                "SELECT * FROM users ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
            return (_row_to_user(row) if row else None, "already_existed")
        return (get_by_username(username), "created")


def get_by_username(username: str) -> User | None:
    row = _conn_get().execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()
    return _row_to_user(row) if row else None


def get_user_count() -> int:
    row = _conn_get().execute("SELECT COUNT(*) FROM users").fetchone()
    return int(row[0]) if row else 0


def get_by_id(uid: str) -> User | None:
    row = _conn_get().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    return _row_to_user(row) if row else None


def authenticate(username: str, password: str) -> User | None:
    user = get_by_username(username)
    if user and user.is_active and verify_password(password, user.hashed_password):
        return user
    return None


def _row_to_user(row: sqlite3.Row) -> User:
    return User(
        id=row["id"],
        username=row["username"],
        hashed_password=row["hashed_password"],
        role=row["role"],
        display_name=row["display_name"],
        created_at=row["created_at"],
        is_active=bool(row["is_active"]),
    )
