"""链路 ID 中间件（P1-2）：入站透传 / 出站回写 / 上下文绑定。

采用纯 ASGI 中间件（而非 `BaseHTTPMiddleware`）是刻意的：后者在部分场景会丢失
contextvars 传播与流式响应能力，而链路追踪的正确性依赖上下文可靠传递。
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from core.logging import TRACE_ID_HEADER, bind_trace, new_trace_id, reset_trace

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


class TraceIdMiddleware:
    """为每个 HTTP 请求生成或透传 trace_id，并绑定到日志上下文。

    - **入站**：优先采用请求头 `X-Trace-Id`（便于跨服务串联），缺失则生成新的；
    - **出站**：回写 `X-Trace-Id` 响应头，便于客户端/网关对齐链路；
    - **清理**：请求结束用 token 精确还原 ContextVar，避免线程复用导致串号。
    """

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        incoming = ""
        for key, value in scope.get("headers", []):
            if key.lower() == TRACE_ID_HEADER.lower().encode():
                incoming = value.decode()
                break
        trace_id = incoming.strip() or new_trace_id()

        tokens = bind_trace(trace_id=trace_id)

        async def send_with_trace_id(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                headers.append((TRACE_ID_HEADER.encode(), trace_id.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_with_trace_id)
        finally:
            reset_trace(*tokens)
