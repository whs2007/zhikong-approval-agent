"""审计写入方式 A/B 基准：逐条 commit vs 组提交。

为什么需要这个脚本
------------------
压测里"吞吐 56 → 139 RPS"是整个系统的观察值，中间隔着 HTTP 层、LangGraph、
SQLite 写锁等一堆变量。要说"提升来自组提交"，必须把变量收敛到只剩写入方式。

做法：同一个库、同一份 schema、同样的 WAL+FULL、同样的并发度，
只改"每条一个事务"还是"一批一个事务"，看耗时差多少。

用法：
    python scripts/bench_audit_writes.py --threads 8 --per-thread 50
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlite3

from core.sqlite_utils import connect

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT, thread_id TEXT, event_type TEXT, actor TEXT, actor_role TEXT,
    node TEXT, phase_before TEXT, phase_after TEXT, detail TEXT, level TEXT,
    created_at TEXT
);
"""

_INSERT = """
INSERT INTO audit_log
(trace_id, thread_id, event_type, actor, actor_role, node,
 phase_before, phase_after, detail, level, created_at)
VALUES (?,?,?,?,?,?,?,?,?,?,?)
"""

_ROW = ("trace", "thread", "NODE_ENTER", "bench", "system", "risk", None, None,
        '{"note":"bench"}', "INFO", "2026-01-01T00:00:00")


def _prepare(path: str) -> sqlite3.Connection:
    if os.path.exists(path):
        os.remove(path)
    conn = connect(path, durable=True)
    conn.executescript(_SCHEMA)
    return conn


def _per_row_commit(path: str, threads: int, per_thread: int) -> float:
    """对照组：每条记录单独一个事务（原实现）。"""
    conn = _prepare(path)
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(per_thread):
            with lock:  # 原实现就是全局锁序列化
                conn.execute(_INSERT, _ROW)
                conn.commit()

    t0 = time.perf_counter()
    ts = [threading.Thread(target=worker) for _ in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    dt = time.perf_counter() - t0
    conn.close()
    return dt


def _batched_commit(path: str, threads: int, per_thread: int, batch: int = 16) -> float:
    """实验组：攒够 batch 条合并成一个事务。"""
    conn = _prepare(path)
    lock = threading.Lock()

    def worker() -> None:
        bucket: list[tuple] = []
        for _ in range(per_thread):
            bucket.append(_ROW)
            if len(bucket) >= batch:
                with lock:
                    conn.executemany(_INSERT, bucket)
                    conn.commit()
                bucket.clear()
        if bucket:
            with lock:
                conn.executemany(_INSERT, bucket)
                conn.commit()

    t0 = time.perf_counter()
    ts = [threading.Thread(target=worker) for _ in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    dt = time.perf_counter() - t0
    conn.close()
    return dt


def _through_group_commit(path: str, threads: int, per_thread: int) -> float:
    """被测实现：真实的 services.audit_service.append（组提交）。"""
    os.environ["AUDIT_DB"] = path
    from core.config import settings

    settings.AUDIT_DB = path
    from services import audit_service

    audit_service._conn = None
    audit_service._queue = []

    def worker(idx: int) -> None:
        for i in range(per_thread):
            audit_service.append("NODE_ENTER", actor=f"u{idx}", thread_id=f"t{idx}-{i}")

    t0 = time.perf_counter()
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    dt = time.perf_counter() - t0
    conn = audit_service._conn
    if conn is not None:
        conn.close()
    return dt


def main() -> int:
    ap = argparse.ArgumentParser(description="审计写入方式 A/B 基准")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--per-thread", type=int, default=50)
    ap.add_argument("--repeat", type=int, default=3, help="每档重复次数，取中位数")
    args = ap.parse_args()

    total = args.threads * args.per_thread
    tmp = tempfile.mkdtemp(prefix="bench-audit-")
    print(f"总记录 {total} 条 = {args.threads} 线程 × {args.per_thread} 条，重复 {args.repeat} 次取中位数")
    print(f"库目录 {tmp}（WAL + synchronous=FULL，与生产一致）\n")

    cases: list[tuple[str, Callable[[str, int, int], float]]] = [
        ("逐条 commit（原实现）", _per_row_commit),
        ("批量 16 条/事务（朴素批）", _batched_commit),
        ("组提交（现实现）", _through_group_commit),
    ]

    results = []
    for name, fn in cases:
        samples = []
        for i in range(args.repeat):
            path = f"{tmp}/{name[:4]}-{i}.db"
            samples.append(fn(path, args.threads, args.per_thread))
        med = statistics.median(samples)
        results.append((name, med))
        print(f"{name:26s} {med * 1000:8.1f} ms   {total / med:8.1f} 条/秒   "
              f"单条 {med / total * 1000:6.3f} ms")

    base = results[0][1]
    print("\n相对原实现的加速：")
    for name, med in results[1:]:
        print(f"  {name:26s} {base / med:5.2f}×")

    print("\n说明：这是纯存储层对照，不含 HTTP / 图执行。"
          "系统级压测的吞吐提升不会等比例等于这里的倍数。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
