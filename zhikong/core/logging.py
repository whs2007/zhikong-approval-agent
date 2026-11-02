"""结构化日志（JSON 行），便于接入 ELK/Loki。

P1-2：全链路 trace_id 注入（修复首轮评审 D1「日志侧链路断裂」）
---------------------------------------------------------------
问题首诊断：`JsonFormatter` 只输出 ts/level/logger/msg，**没有 trace_id 字段**；
各节点日志靠 `f"[analyst] thread={tid}"` 手动拼接 id，HTTP 请求层也没有中间件，
导致「一次请求 → N 个节点日志 → M 条审计」无法用统一 id 串联查询。

本模块通过 ContextVar 承载链路上下文，并由 `TraceContextFilter` 自动注入每条日志：
- `bind_trace(trace_id=..., thread_id=...)` 绑定上下文（由 ASGI 中间件 / 工作流服务调用）；
- 所有 `logger.info(...)` 无需任何改动即自动携带 trace_id / thread_id；
- `JsonFormatter` 仅在有值时输出这两个字段，避免无链路场景产生噪声。
"""
from __future__ import annotations

import json
import logging
import sys
import uuid
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import datetime

# 链路 ID 的请求头约定：入站透传优先，出站回写以便网关/客户端对齐
TRACE_ID_HEADER = "X-Trace-Id"

_trace_id_var: ContextVar[str] = ContextVar("zhikong_trace_id", default="")
_thread_id_var: ContextVar[str] = ContextVar("zhikong_thread_id", default="")


def new_trace_id() -> str:
    return uuid.uuid4().hex


def bind_trace(
    *, trace_id: str | None = None, thread_id: str | None = None
) -> tuple[Token[str] | None, Token[str] | None]:
    """把 trace_id / thread_id 绑定到当前上下文，返回用于精确还原的 token。

    只绑定显式传入的项（传 None 表示不动该项）。请求结束务必调用 `reset_trace()`
    归还 token，否则复用连接/线程时会串号。
    """
    tid_token = None
    th_token = None
    if trace_id is not None:
        tid_token = _trace_id_var.set(trace_id)
    if thread_id is not None:
        th_token = _thread_id_var.set(thread_id)
    return tid_token, th_token


def reset_trace(tid_token: Token[str] | None = None, th_token: Token[str] | None = None) -> None:
    """用 `bind_trace` 返回的 token 精确还原上下文。"""
    if tid_token is not None:
        _trace_id_var.reset(tid_token)
    if th_token is not None:
        _thread_id_var.reset(th_token)


@contextmanager
def trace_scope(*, trace_id: str | None = None, thread_id: str | None = None):
    """不依赖 Token 的链路作用域，专供**跨执行上下文迭代的生成器**使用。

    为什么需要另一个 API：`bind_trace` 返回的 `Token` 与"创建它的 Context"
    强绑定。SSE 场景下同步生成器由 `iterate_in_threadpool` 驱动，每次 `next()`
    可能落在不同的 Context 副本中，退出时 `reset(token)` 会抛
    `ValueError: Token was created in a different Context`（实测：流已正常发完，
    却在 finally 抛异常，把 200 变成 500）。

    `ContextVar.set()` 不校验来源 Context，因此这里改为"记旧值 → set → 还原旧值"。
    """
    old_trace = _trace_id_var.get()
    old_thread = _thread_id_var.get()
    if trace_id is not None:
        _trace_id_var.set(trace_id)
    if thread_id is not None:
        _thread_id_var.set(thread_id)
    try:
        yield
    finally:
        _trace_id_var.set(old_trace)
        _thread_id_var.set(old_thread)


def get_trace_id() -> str:
    return _trace_id_var.get()


def get_thread_id() -> str:
    return _thread_id_var.get()


class TraceContextFilter(logging.Filter):
    """把 ContextVar 中的链路 ID 注入 LogRecord。

    业务代码因此不必手动拼 `[analyst] thread=...`，日志格式统一且不会漏传。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = _trace_id_var.get()
        record.thread_id = _thread_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.utcfromtimestamp(record.created).isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # 链路字段：仅在有值时输出，避免无上下文时产生空字段噪声
        trace_id = getattr(record, "trace_id", "")
        if trace_id:
            payload["trace_id"] = trace_id
        thread_id = getattr(record, "thread_id", "")
        if thread_id:
            payload["thread_id"] = thread_id
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def get_logger(name: str = "zhikong") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    # 幂等地为每个 handler 装配链路 Filter（重复调用 get_logger 也不丢失）
    for h in logger.handlers:
        if not any(isinstance(f, TraceContextFilter) for f in h.filters):
            h.addFilter(TraceContextFilter())
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


logger = get_logger("zhikong")
