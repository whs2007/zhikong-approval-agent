"""审计 group commit：语义不变（返回即落盘）+ 并发合并提交。

这组测试守的是两件事，缺一不可：
1. **不能为了快牺牲语义** —— append() 返回时，数据必须已经能被另一个连接读到。
   如果哪天有人把它改成"丢进队列就返回"，第一个用例就会红。
2. **合并确实发生** —— 并发写 N 条时 commit 次数必须显著小于 N。
   否则"组提交"只是换了个名字的同步写，性能优化是假的。
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from services import audit_service


class _CommitSpy:
    """代理 sqlite 连接，只拦截 commit 做计数/注入故障。

    不能直接 patch `conn.commit`：CPython 里 sqlite3.Connection 的方法是只读属性。
    """

    def __init__(self, inner: sqlite3.Connection) -> None:
        self._inner = inner
        self.commits = 0
        self.fail = False

    def commit(self) -> None:
        self.commits += 1
        if self.fail:
            raise sqlite3.OperationalError("disk I/O error")
        self._inner.commit()

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


@pytest.fixture()
def fresh_audit(tmp_path, monkeypatch):
    """把审计库指向临时文件，并重置模块级单例，避免污染其它用例。

    注意：**不重置 `_flusher`**。提交线程是常驻的，重置引用会让下一个 append
    再起一个线程，两个线程抢同一个队列，测试会随机拿到空 batch 而假失败。
    """
    monkeypatch.setattr(audit_service, "_conn", None)
    monkeypatch.setattr(audit_service, "_queue", [])
    monkeypatch.setattr(audit_service.settings, "AUDIT_DB", str(tmp_path / "audit.db"))
    yield tmp_path / "audit.db"
    # 收尾：把残留连接关掉，防止 fd 泄漏影响后续用例
    conn = audit_service._conn
    if conn is not None:
        with audit_service._db_lock:
            conn.close()
    monkeypatch.setattr(audit_service, "_conn", None)


def _count(db_path) -> int:
    c = sqlite3.connect(db_path)
    try:
        return c.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    finally:
        c.close()


def test_append_is_durable_when_it_returns(fresh_audit):
    """同步语义：append 返回后，另一个连接能读到（不是"稍后异步落盘"）。"""
    audit_service.append("WORKFLOW_SUBMIT", actor="u1", thread_id="t-1")
    # 用一个全新的连接读，绕开模块内连接与任何缓存
    assert _count(fresh_audit) == 1


def test_group_commit_merges_concurrent_appends(fresh_audit, monkeypatch):
    """并发写 100 条，commit 次数应远小于 100。"""
    threads_n, per_thread = 10, 10
    total = threads_n * per_thread

    with audit_service._db_lock:
        spy = _CommitSpy(audit_service._get_conn())
    monkeypatch.setattr(audit_service, "_conn", spy)

    def worker(idx: int) -> None:
        for i in range(per_thread):
            audit_service.append("NODE_ENTER", actor=f"u{idx}", thread_id=f"t{idx}-{i}")

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(threads_n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert _count(fresh_audit) == total, "并发写入丢了记录"
    assert spy.commits < total / 2, f"提交没有合并：{spy.commits} 次 commit / {total} 条"
    assert spy.commits >= 1


def test_append_degrades_on_commit_failure(fresh_audit, monkeypatch):
    """落盘失败不能让业务停摆：append 只告警，不抛异常。"""
    with audit_service._db_lock:
        spy = _CommitSpy(audit_service._get_conn())
    spy.fail = True
    monkeypatch.setattr(audit_service, "_conn", spy)

    audit_service.append("WORKFLOW_DONE", thread_id="t-fail")  # 不应抛出
    assert _count(fresh_audit) == 0
    assert spy.commits == 1
