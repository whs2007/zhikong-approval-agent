"""执行幂等台账（Execution Ledger）。

目的（P0-2）：保证同一 `idempotency_key` 的「成功副作用」只发生一次。
- 执行节点在真正产生副作用**之前**先以 pending 状态占坑（claim_pending），
  成功后再把状态更新为 success（update_status）。
- 这样消除了「执行成功 → 进程崩溃 → 重放再次执行」的窗口：若崩溃发生在
  「执行后 / 更新 success 前」，重放时看到 status=pending（而非 success），
  会安全地重新执行一次（真实业务副作用应具备幂等性）。
- 若已 success，则直接返回缓存结果，绝不重复副作用。
- 台账落独立 SQLite 文件（与 checkpoint 同目录），append 语义，主键去重。
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from core.config import settings
from core.sqlite_utils import connect

_DDL = """
CREATE TABLE IF NOT EXISTS execution_ledger (
    idempotency_key TEXT PRIMARY KEY,
    thread_id       TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending | success
    result          TEXT,
    payload         TEXT,
    created_at      TEXT
);
"""

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _db_path() -> str:
    # 与 checkpointer 同目录，便于随环境一起隔离（测试用临时目录）
    return str(Path(settings.CHECKPOINTER_DB).with_name("ledger.db"))


def _c() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        Path(_db_path()).parent.mkdir(parents=True, exist_ok=True)
        _conn = connect(_db_path(), durable=True, row_factory=True)
        _conn.executescript(_DDL)
        # 迁移：升级前已存在的台账表可能无 status 列，需补列以免 INSERT/UPDATE 失败
        cols = [r[1] for r in _conn.execute("PRAGMA table_info(execution_ledger)").fetchall()]
        if "status" not in cols:
            _conn.execute(
                "ALTER TABLE execution_ledger ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'"
            )
            _conn.commit()
    return _conn


def get(key: str) -> dict | None:
    row = _c().execute(
        "SELECT * FROM execution_ledger WHERE idempotency_key = ?", (key,)
    ).fetchone()
    return dict(row) if row else None


def record(key: str, thread_id: str, result: str, payload: str = "") -> None:
    """向后兼容：直接登记为 success（等价于 claim_pending + update_status）。

    新代码应优先使用 claim_pending（执行前占坑）+ update_status（成功后更新），
    以关闭崩溃窗口。
    """
    claim_pending(key, thread_id)
    update_status(key, "success", result, payload)


def claim_pending(key: str, thread_id: str) -> bool:
    """执行副作用**之前**占坑：以 pending 状态登记，主键去重。

    返回 True 表示本次首次占有（全新执行）；False 表示已存在
    （重复提交，或上次执行到一半崩溃后的重放）——两种情况都应继续安全执行。
    """
    with _lock:
        conn = _c()
        cur = conn.execute(
            "INSERT OR IGNORE INTO execution_ledger "
            "(idempotency_key, thread_id, status, result, payload, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (key, thread_id, "pending", "", "", datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
        return cur.rowcount > 0


def update_status(key: str, status: str, result: str = "", payload: str = "") -> None:
    """更新某 key 的执行状态（典型为 success）。"""
    with _lock:
        conn = _c()
        conn.execute(
            "UPDATE execution_ledger SET status=?, result=?, payload=? "
            "WHERE idempotency_key=?",
            (status, result, payload, key),
        )
        conn.commit()


def count() -> int:
    row = _c().execute("SELECT COUNT(*) FROM execution_ledger").fetchone()
    return int(row[0]) if row else 0
