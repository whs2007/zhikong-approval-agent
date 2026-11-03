"""证明「异步批量写」会丢审计，而「组提交」不会。

场景：连续写 N 条审计，写到第 K 条时进程被 SIGKILL（模拟 OOM / kill -9 / 容器被驱逐）。

- async  ：append 只入队就返回，后台线程隔一会儿批量落盘 → 返回≠落盘，被杀就丢
- group  ：append 阻塞到 commit 完成才返回 → 返回=落盘，被杀不丢

用法：
    python scripts/proof_audit_loss.py async 200
    python scripts/proof_audit_loss.py group 200
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, trace_id TEXT, thread_id TEXT,
    event_type TEXT, actor TEXT, actor_role TEXT, node TEXT,
    phase_before TEXT, phase_after TEXT, detail TEXT, level TEXT, created_at TEXT
);
"""
_INSERT = """
INSERT INTO audit_log
(trace_id, thread_id, event_type, actor, actor_role, node,
 phase_before, phase_after, detail, level, created_at)
VALUES (?,?,?,?,?,?,?,?,?,?,?)
"""


class AsyncAudit:
    """反例：把写入丢进队列就返回（很多"高性能审计"是这么写的）。"""

    def __init__(self, path: str) -> None:
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.executescript(_SCHEMA)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.q: list[tuple] = []
        self.lock = threading.Lock()
        self.stop = False
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while not self.stop:
            time.sleep(0.2)  # 攒一会儿再批量写
            with self.lock:
                if not self.q:
                    continue
                batch, self.q[:] = list(self.q), []
            self.conn.executemany(_INSERT, batch)
            self.conn.commit()

    def append(self) -> None:
        with self.lock:
            self.q.append(("t", "th", "NODE_ENTER", "u", "system", "n", None, None, "", "INFO", "ts"))


def main() -> int:
    if len(sys.argv) < 3:
        print("用法: proof_audit_loss.py <async|group> <n>")
        return 2
    mode, n = sys.argv[1], int(sys.argv[2])
    db = os.path.join(tempfile.mkdtemp(prefix="proof-"), "audit.db")

    if mode == "async":
        w = AsyncAudit(db)

        def append_one() -> None:
            w.append()
    else:
        from core.config import settings
        from services import audit_service

        settings.AUDIT_DB = db
        audit_service._conn = None
        audit_service._queue = []
        audit_service.append("NODE_ENTER")  # 建表

        def append_one() -> None:
            audit_service.append("NODE_ENTER", thread_id="th")

    for i in range(n):
        append_one()
        if i == n // 2:  # 一半时被 SIGKILL
            print(f"[{mode}] 已写入 {i + 1} 条，此刻被 SIGKILL", flush=True)
            os.kill(os.getpid(), 9)  # 不走 atexit、不 flush
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
