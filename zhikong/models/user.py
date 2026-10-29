"""用户与角色模型（RBAC）。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class User:
    id: str
    username: str
    hashed_password: str
    role: str  # applicant | approver | admin
    display_name: str
    created_at: str
    is_active: bool = True

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "role": self.role,
            "display_name": self.display_name,
            "is_active": self.is_active,
        }


# 用户表 DDL（由 AuthService 建表）
USER_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,
    username        TEXT UNIQUE NOT NULL,
    hashed_password TEXT NOT NULL,
    role            TEXT NOT NULL,
    display_name    TEXT,
    created_at      TEXT,
    is_active       INTEGER DEFAULT 1
);
"""
