"""Prometheus 真实指标（P1-3）。

背景（复验 R3-2 / D3 短板）
-------------------------
原 `/metrics` 是对审计表做全量 COUNT 的 JSON 接口：既不是 Prometheus 格式，也没有
延迟、成功率、工具调用等任何可量化指标——线上既无法告警，也无法定位容量拐点。
本模块提供标准 Prometheus Registry 与真实埋点，覆盖四类关键信号：

1. **流量/延迟**：HTTP 请求计数与耗时直方图（Prometheus 侧可算 QPS 与 P95/P99）；
2. **业务结果**：工作流提交与终态（成功/失败/驳回）计数，可算成功率；
3. **工具韧性**：外部工具调用结果与耗时，配合 `zhikong_breaker_open` 观察熔断；
4. **积压**：待人工审批数 Gauge，用于"审批积压"告警。

设计要点
--------
- 使用**独立 CollectorRegistry**（而非全局默认 REGISTRY），避免多进程/多 Registry
  场景下的重复注册冲突，也便于测试隔离。
- HTTP 路径标签必须**归一化**：原始路径含 thread_id 会造成标签基数爆炸（series 失控），
  因此把 WF-xxx / 长 hex 片断替换为占位符。
"""
from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

REGISTRY = CollectorRegistry()

HTTP_REQUESTS = Counter(
    "zhikong_http_requests_total",
    "HTTP 请求总数",
    ["method", "path", "status"],
    registry=REGISTRY,
)

HTTP_DURATION = Histogram(
    "zhikong_http_request_duration_seconds",
    "HTTP 请求耗时（秒）",
    ["method", "path"],
    registry=REGISTRY,
)

WORKFLOW_SUBMISSIONS = Counter(
    "zhikong_workflow_submissions_total",
    "工作流提交数（按初态：execute=自动放行 / approve=需审批）",
    ["initial_phase"],
    registry=REGISTRY,
)

WORKFLOW_FINAL = Counter(
    "zhikong_workflow_final_total",
    "工作流终态统计（success / failed / rejected / unknown）",
    ["result"],
    registry=REGISTRY,
)

WORKFLOW_INVOKE_DURATION = Histogram(
    "zhikong_workflow_invoke_duration_seconds",
    "单次工作流图执行耗时（秒）",
    registry=REGISTRY,
)

TOOL_CALLS = Counter(
    "zhikong_tool_calls_total",
    "外部工具调用结果统计（success / failed / breaker_open）",
    ["tool", "outcome"],
    registry=REGISTRY,
)

TOOL_CALL_DURATION = Histogram(
    "zhikong_tool_call_duration_seconds",
    "单次外部工具调用耗时（秒）",
    ["tool"],
    registry=REGISTRY,
)

BREAKER_OPEN = Gauge(
    "zhikong_breaker_open",
    "熔断器是否开路（1=开路，下游已被判定不可用）",
    registry=REGISTRY,
)

OVERDUE_APPROVALS = Gauge(
    "zhikong_overdue_approvals",
    "已超过审批时限仍未处理的工作流数（超时积压告警）",
    registry=REGISTRY,
)

PENDING_APPROVALS = Gauge(
    "zhikong_pending_approvals",
    "当前等待人工审批的工作流数（用于审批积压告警）",
    registry=REGISTRY,
)

# ---- LLM 调用治理（P1-4） ----

LLM_CALLS = Counter(
    "zhikong_llm_calls_total",
    "LLM 调用结果统计（success / failed / fallback / throttled）",
    ["provider", "outcome"],
    registry=REGISTRY,
)

LLM_CALL_DURATION = Histogram(
    "zhikong_llm_call_duration_seconds",
    "单次 LLM 调用耗时（秒，含重试）",
    ["provider"],
    registry=REGISTRY,
)

LLM_INFLIGHT = Gauge(
    "zhikong_llm_inflight",
    "当前并发占用 LLM 的调用数（配合 LLM_MAX_CONCURRENCY 观察是否打满）",
    registry=REGISTRY,
)

LLM_TOKENS = Counter(
    "zhikong_llm_tokens_total",
    "LLM token 消耗（按 provider 与类型：prompt / completion）",
    ["provider", "type"],
    registry=REGISTRY,
)


# ----------------------------- 路径归一化 -----------------------------

_WF_ID_RE = re.compile(r"\bWF-[0-9a-zA-Z]{6,32}\b")
_LONG_ID_RE = re.compile(r"\b[0-9a-fA-F]{8,}\b")


def normalize_path(path: str) -> str:
    """把路径中的动态 ID 替换为占位符，避免 Prometheus 标签基数爆炸。

    例：`/api/v1/approval/decide/WF-4f96d403fbc2` -> `/api/v1/approval/decide/{WF}`
    """
    path = _WF_ID_RE.sub("{WF}", path)
    return _LONG_ID_RE.sub("{id}", path)


# ----------------------------- HTTP 中间件 -----------------------------

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


class MetricsMiddleware:
    """记录 HTTP 请求计数与耗时。纯 ASGI 实现以保证 contextvars 与流式响应正确。"""

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "")
        path = normalize_path(scope.get("path", ""))
        started = time.perf_counter()
        status = "500"  # 异常未产生响应时按 500 计

        async def send_wrapper(message: dict[str, Any]) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = str(message.get("status", 500))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            HTTP_REQUESTS.labels(method=method, path=path, status=status).inc()
            HTTP_DURATION.labels(method=method, path=path).observe(
                time.perf_counter() - started
            )


# ----------------------------- 埋点辅助 -----------------------------

def record_workflow_submission(initial_phase: str) -> None:
    WORKFLOW_SUBMISSIONS.labels(initial_phase=initial_phase or "unknown").inc()


def record_workflow_final(result: str) -> None:
    WORKFLOW_FINAL.labels(result=result or "unknown").inc()


def observe_invoke_duration(seconds: float) -> None:
    WORKFLOW_INVOKE_DURATION.observe(max(0.0, seconds))


def record_tool_call(tool: str, outcome: str, duration_seconds: float = 0.0) -> None:
    TOOL_CALLS.labels(tool=tool or "unknown", outcome=outcome or "unknown").inc()
    TOOL_CALL_DURATION.labels(tool=tool or "unknown").observe(max(0.0, duration_seconds))


def set_breaker_open(is_open: bool) -> None:
    BREAKER_OPEN.set(1 if is_open else 0)


def set_overdue_approvals(count: int) -> None:
    OVERDUE_APPROVALS.set(max(0, int(count)))


def set_pending_approvals(count: int) -> None:
    PENDING_APPROVALS.set(max(0, int(count)))


def record_llm_call(provider: str, outcome: str, duration_seconds: float = 0.0) -> None:
    """记录一次 LLM 调用的最终结果与耗时（耗时含重试）。"""
    prov = provider or "unknown"
    LLM_CALLS.labels(provider=prov, outcome=outcome or "unknown").inc()
    LLM_CALL_DURATION.labels(provider=prov).observe(max(0.0, duration_seconds))


def record_llm_tokens(provider: str, *, prompt_tokens: int = 0,
                      completion_tokens: int = 0) -> None:
    """记录一次 LLM 调用的 token 消耗（prompt / completion 分开计）。

    规则兜底（RuleBasedLLM）不消耗 token，调用方传 0 即可；真实 provider
    在响应里拿到 usage 后回传，便于在 Grafana 看 token 成本。
    """
    prov = provider or "unknown"
    if prompt_tokens:
        LLM_TOKENS.labels(provider=prov, type="prompt").inc(prompt_tokens)
    if completion_tokens:
        LLM_TOKENS.labels(provider=prov, type="completion").inc(completion_tokens)


def render_metrics() -> tuple[bytes, str]:
    """渲染 Prometheus 文本格式响应体与 content-type。"""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
