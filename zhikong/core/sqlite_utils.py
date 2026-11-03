"""SQLite 连接统一入口：把 WAL / synchronous / busy_timeout 收在一处。

为什么需要统一
--------------
压测（docs/21）测出单次 commit 的成本完全由这三个 PRAGMA 决定：

    delete + FULL   ≈ 13–18 ms   ← 业务库/幂等账本原来是这个
    wal    + FULL   ≈ 4.6 ms
    wal    + NORMAL ≈ 0.008 ms

三个库各写各的（审计开了 WAL，业务库和账本连 WAL 都没开），导致同一份代码在不同
机器上吞吐能差 4 倍，而且没人知道差在哪。统一之后，"慢"只可能来自磁盘本身。

durability 分级（不是所有数据都值得每次 fsync）
------------------------------------------------
- durable=True：审计、幂等账本、用户。断电不能丢，用 WAL + FULL。
- durable=False：LangGraph checkpoint。可从审计事件重建，断电最多丢最后几个事务、
  库不会损坏，因此用 WAL + NORMAL 换吞吐。

这不是"为了快牺牲正确性"：丢的是可重建的中间状态，不可重建的业务事实仍是 FULL。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def connect(path: str, *, durable: bool = True, row_factory: bool = False) -> sqlite3.Connection:
    """打开一个已配置好的 SQLite 连接。

    Args:
        path: 数据库文件路径；`:memory:` 或 URI 形式会跳过文件相关设置。
        durable: True 用 synchronous=FULL（掉电不丢已提交事务），
                 False 用 NORMAL（更快，掉电可能丢最后若干事务，库不损坏）。
        row_factory: 是否用 sqlite3.Row（按列名取值）；checkpointer 用默认 tuple。
    """
    is_memory = path == ":memory:" or path.startswith("file:")
    if not is_memory:
        Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path, check_same_thread=False)
    if row_factory:
        conn.row_factory = sqlite3.Row
    # 内存库不支持 WAL，设置会报错或静默无效，直接跳过
    if not is_memory:
        conn.execute("PRAGMA journal_mode=WAL")
    # 多进程/多线程同时写时，不要立刻抛 "database is locked"，先等一会儿
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(f"PRAGMA synchronous={'FULL' if durable else 'NORMAL'}")
    return conn
