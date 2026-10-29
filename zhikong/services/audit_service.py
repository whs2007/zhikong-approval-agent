"""审计子系统（功能性审计核心）。

设计要点：
- 独立 SQLite 文件（append-only），与业务库/Checkpointer 物理分离。
- 仅提供 INSERT（append），无 UPDATE/DELETE 接口，保证不可篡改。
- 支持多条件查询与 CSV/JSON 导出，满足合规留痕。
- 写入加锁，兼容多线程（FastAPI 线程池）并发。
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
import threading
import uuid
from datetime import datetime
from typing import Any, Optional

from core.config import settings
from core.logging import logger

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id      TEXT,
    thread_id     TEXT,
    event_type    TEXT,
    actor         TEXT,
    actor_role    TEXT,
    node          TEXT,
    phase_before  TEXT,
    phase_after   TEXT,
    detail        TEXT,
    level         TEXT,
    created_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_thread ON audit_log(thread_id);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_event ON audit_log(event_type);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor);
"""

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(settings.AUDIT_DB, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL;")
        _conn.executescript(_SCHEMA)
    return _conn


# ----------------------- 审计事件类型常量 -----------------------
class EventType:
    WORKFLOW_SUBMIT = "WORKFLOW_SUBMIT"
    NODE_ENTER = "NODE_ENTER"
    NODE_EXIT = "NODE_EXIT"
    RISK_ANALYZED = "RISK_ANALYZED"
    APPROVAL_PAUSED = "APPROVAL_PAUSED"
    APPROVAL_DECISION = "APPROVAL_DECISION"
    EXEC_START = "EXEC_START"
    EXEC_RESULT = "EXEC_RESULT"
    RETRY = "RETRY"
    WORKFLOW_DONE = "WORKFLOW_DONE"
    AUTH_LOGIN = "AUTH_LOGIN"
    AUTH_FAIL = "AUTH_FAIL"
    ADMIN_BOOTSTRAP = "ADMIN_BOOTSTRAP"
    ADMIN_CREATED = "ADMIN_CREATED"
    KNOWLEDGE_RELOAD = "KNOWLEDGE_RELOAD"


def new_trace_id() -> str:
    return uuid.uuid4().hex


def append(
    event_type: str,
    *,
    actor: str = "system",
    actor_role: str = "system",
    thread_id: str | None = None,
    trace_id: str | None = None,
    node: str | None = None,
    phase_before: str | None = None,
    phase_after: str | None = None,
    detail: Any = None,
    level: str = "INFO",
) -> None:
    """写入一条审计事件。失败仅告警，不影响主流程（降级）。"""
    try:
        detail_str = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
        ts = datetime.now().isoformat(timespec="seconds")
        with _lock:
            _get_conn().execute(
                """
                INSERT INTO audit_log
                (trace_id, thread_id, event_type, actor, actor_role, node,
                 phase_before, phase_after, detail, level, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    trace_id,
                    thread_id,
                    event_type,
                    actor,
                    actor_role,
                    node,
                    phase_before,
                    phase_after,
                    detail_str,
                    level,
                    ts,
                ),
            )
            _get_conn().commit()
    except Exception as e:  # pragma: no cover - 审计降级
        logger.warning(f"[audit] append failed: {e}")


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


def query(
    *,
    thread_id: str | None = None,
    actor: str | None = None,
    event_type: str | None = None,
    start: str | None = None,
    end: str | None = None,
    page: int = 1,
    size: int = 50,
) -> dict:
    conds, params = [], []
    if thread_id:
        conds.append("thread_id = ?"); params.append(thread_id)
    if actor:
        conds.append("actor = ?"); params.append(actor)
    if event_type:
        conds.append("event_type = ?"); params.append(event_type)
    if start:
        conds.append("created_at >= ?"); params.append(start)
    if end:
        conds.append("created_at <= ?"); params.append(end)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    con = _get_conn()
    total = con.execute(f"SELECT COUNT(*) FROM audit_log {where}", params).fetchone()[0]
    rows = con.execute(
        f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        params + [size, max(0, (page - 1) * size)],
    ).fetchall()
    return {"items": [_row_to_dict(r) for r in rows], "total": total}


def trace(trace_id: str) -> list[dict]:
    rows = _get_conn().execute(
        "SELECT * FROM audit_log WHERE trace_id = ? ORDER BY id ASC", (trace_id,)
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def export_csv(filters: dict) -> str:
    data = query(**filters, size=100000)["items"]
    buf = io.StringIO()
    writer = csv.writer(buf)
    cols = ["id", "trace_id", "thread_id", "event_type", "actor", "actor_role",
            "node", "phase_before", "phase_after", "detail", "level", "created_at"]
    writer.writerow(cols)
    for item in data:
        writer.writerow([item.get(c, "") for c in cols])
    return buf.getvalue()


def export_json(filters: dict) -> str:
    data = query(**filters, size=100000)["items"]
    return json.dumps(data, ensure_ascii=False, indent=2)
