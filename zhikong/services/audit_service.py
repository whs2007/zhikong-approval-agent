"""审计子系统（功能性审计核心）。

设计要点：
- 独立 SQLite 文件（append-only），与业务库/Checkpointer 物理分离。
- 仅提供 INSERT（append），无 UPDATE/DELETE 接口，保证不可篡改。
- 支持多条件查询与 CSV/JSON 导出，满足合规留痕。
- 写入走 group commit（组提交），兼容多线程（FastAPI 线程池）并发。

为什么是 group commit 而不是异步队列
------------------------------------
一次审批提交会写 ~13 条审计事件。每条都单独 `INSERT + commit`，在 WAL+FULL 下
每次 commit 要等一次 fsync（本机实测 4.6ms），13 次就是几十毫秒——压测里审计是
单请求成本的主要构成（见 docs/21、docs/23）。

常见的"优化"是改成异步队列 + 批量落盘，但那会**改变语义**：`append()` 返回时
数据还在内存里，进程被 kill 就丢审计，而审计是这套系统唯一的不可重建事实来源。

group commit 保留同步语义：调用方仍然阻塞到"自己这条已 commit"才返回，只是把
同一瞬间到达的 N 条合并进一个事务、共用一次 fsync。N 条的成本 ≈ 1 条。
"""

from __future__ import annotations

import atexit
import csv
import io
import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from typing import Any

from core.config import settings
from core.logging import logger
from core.sqlite_utils import connect

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

_INSERT_SQL = """
INSERT INTO audit_log
(trace_id, thread_id, event_type, actor, actor_role, node,
 phase_before, phase_after, detail, level, created_at)
VALUES (?,?,?,?,?,?,?,?,?,?,?)
"""

# 攒批窗口：队列不足 _TARGET_BATCH 时，最多等这么久看有没有后续到达。
# 取 200µs —— 远小于一次 fsync（4.6ms），对单请求延迟几乎不可见，
# 却足以把"同一瞬间并发到达"的写入合并掉。
# 队列一旦攒够 _TARGET_BATCH 就立刻提交，不再空等（早期版本无脑等满窗口，
# 实测比朴素批量慢 2.3 倍，见 docs/23）。
_BATCH_WINDOW_SECONDS = 0.0002
_TARGET_BATCH = 8

# 调用方等待落盘的上限。超过即降级（告警但不阻塞业务），避免 flusher 异常时请求挂死。
_FLUSH_WAIT_TIMEOUT = 10.0

_db_lock = threading.Lock()  # 保护 sqlite 连接本身（写入与查询共用）
_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    """取连接。调用方须持有 _db_lock。"""
    global _conn
    if _conn is None:
        _conn = connect(settings.AUDIT_DB, durable=True, row_factory=True)
        _conn.executescript(_SCHEMA)
    return _conn


class _Pending:
    """一条待落盘的审计记录 + 它自己的完成信号。"""

    __slots__ = ("done", "error", "row")

    def __init__(self, row: tuple) -> None:
        self.row = row
        self.done = threading.Event()
        self.error: str | None = None


_queue: list[_Pending] = []
_qcv = threading.Condition()
_flusher: threading.Thread | None = None
_stopping = False


def _ensure_flusher() -> None:
    """惰性启动后台提交线程。调用方须持有 _qcv。

    必须带 is_alive 判断：两个线程共享同一个 _queue，一旦并存就会出现
    "A 清空队列后 B 拿到空 batch" 的情况，白付一次 commit。
    """
    global _flusher
    if _flusher is None or not _flusher.is_alive():
        _flusher = threading.Thread(target=_flush_loop, name="audit-committer", daemon=True)
        _flusher.start()


def _flush_loop() -> None:
    while True:
        with _qcv:
            while not _queue and not _stopping:
                _qcv.wait()
            if not _queue:
                return  # stopping 且队列空：退出
            # 攒批：队列已够一批就立即走；否则最多等一个窗口，把"同时到达"的合并。
            # 上限只加在等待上，不加在提交上——持续高并发时不会因为窗口而推迟。
            if len(_queue) < _TARGET_BATCH:
                deadline = time.monotonic() + _BATCH_WINDOW_SECONDS
                while len(_queue) < _TARGET_BATCH:
                    remain = deadline - time.monotonic()
                    if remain <= 0:
                        break
                    _qcv.wait(remain)
            batch, _queue[:] = list(_queue), []
        if not batch:  # 竞争下可能拿到空批，不要为它付一次 fsync
            continue

        err: str | None = None
        try:
            with _db_lock:
                conn = _get_conn()
                conn.executemany(_INSERT_SQL, [it.row for it in batch])
                conn.commit()  # 一次 commit = 一次 fsync，N 条共用
        except Exception as e:  # pragma: no cover - 磁盘故障等
            err = str(e)
            logger.warning(f"[audit] batch commit failed ({len(batch)} rows): {e}")
        finally:
            for it in batch:
                it.error = err
                it.done.set()


def _drain_on_exit() -> None:
    """进程退出前把队列里剩下的记录写完，避免丢审计。"""
    global _stopping
    with _qcv:
        _stopping = True
        _qcv.notify_all()
    if _flusher is not None:
        _flusher.join(timeout=5.0)


atexit.register(_drain_on_exit)


# ----------------------- 审计事件类型常量 -----------------------
class EventType:
    WORKFLOW_SUBMIT = "WORKFLOW_SUBMIT"
    NODE_ENTER = "NODE_ENTER"
    NODE_EXIT = "NODE_EXIT"
    RISK_ANALYZED = "RISK_ANALYZED"
    APPROVAL_PAUSED = "APPROVAL_PAUSED"
    APPROVAL_DECISION = "APPROVAL_DECISION"
    APPROVAL_ESCALATED = "APPROVAL_ESCALATED"
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
    """写入一条审计事件。返回时该条已 commit。

    失败仅告警，不影响主流程（降级）——审计不可用不应让业务停摆，
    但会留下一条 warning 供监控发现。
    """
    try:
        detail_str = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
        ts = datetime.now().isoformat(timespec="seconds")
        item = _Pending(
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
            )
        )
        with _qcv:
            _queue.append(item)
            _ensure_flusher()
            _qcv.notify()
        if not item.done.wait(_FLUSH_WAIT_TIMEOUT):  # pragma: no cover - flusher 卡死
            logger.warning(f"[audit] append timeout waiting for commit: {event_type}")
        elif item.error:  # pragma: no cover
            logger.warning(f"[audit] append failed: {item.error}")
    except Exception as e:  # pragma: no cover - 审计降级
        logger.warning(f"[audit] append failed: {e}")


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


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
    conds: list[str] = []
    params: list[Any] = []
    if thread_id:
        conds.append("thread_id = ?")
        params.append(thread_id)
    if actor:
        conds.append("actor = ?")
        params.append(actor)
    if event_type:
        conds.append("event_type = ?")
        params.append(event_type)
    if start:
        conds.append("created_at >= ?")
        params.append(start)
    if end:
        conds.append("created_at <= ?")
        params.append(end)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    with _db_lock:
        con = _get_conn()
        total = con.execute(f"SELECT COUNT(*) FROM audit_log {where}", params).fetchone()[0]
        rows = con.execute(
            f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            [*params, size, max(0, (page - 1) * size)],
        ).fetchall()
    return {"items": [_row_to_dict(r) for r in rows], "total": total}


def trace(trace_id: str) -> list[dict]:
    with _db_lock:
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
