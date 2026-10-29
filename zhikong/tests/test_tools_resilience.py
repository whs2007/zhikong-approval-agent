"""P1-1 工具调用韧性测试：超时 + 指数退避重试 + 熔断 + 错误分类。"""
from __future__ import annotations

import sys
import time

import pytest

sys.path.insert(0, ".")

from core.config import settings
from workflows.tools import (
    BreakerState,
    CircuitBreaker,
    CircuitBreakerOpen,
    SupplierOrderTool,
    ToolError,
    ToolErrorKind,
    backoff_delay,
    call_with_timeout,
    run_with_resilience,
)

# ----------------------------- 测试替身 -----------------------------

class OkTool:
    name = "ok"

    def call(self, payload, timeout=None):
        return "成功"


class FailTool:
    """总是重试型失败；记录 call 次数以便断言重试行为。"""

    name = "fail"

    def __init__(self, error: ToolError | None = None, calls: list | None = None):
        self.calls = calls if calls is not None else []
        self.error = error or ToolError("模拟超时", retryable=True, kind=ToolErrorKind.RETRYABLE)

    def call(self, payload, timeout=None):
        self.calls.append(time.monotonic())
        raise self.error


class SlowTool:
    """睡眠超过 timeout，用于触发超时分支。"""

    name = "slow"

    def __init__(self, delay: float = 0.5):
        self.delay = delay

    def call(self, payload, timeout=None):
        time.sleep(self.delay)
        return "不该返回"


# ----------------------------- 超时 -----------------------------

def test_call_with_timeout_raises_timeout_error():
    """反例：工具阻塞超过 timeout 必须被中断为 ToolError(timeout) 而非无限等待。"""
    with pytest.raises(ToolError) as ei:
        call_with_timeout(SlowTool(delay=1.0), {}, timeout=0.1)
    assert ei.value.kind is ToolErrorKind.TIMEOUT
    assert ei.value.retryable is True  # 超时是可重试的瞬时故障


def test_slow_tool_within_timeout_succeeds():
    tool = SlowTool(delay=0.01)
    assert call_with_timeout(tool, {}, timeout=2.0) == "不该返回"


# ----------------------------- 退避重试 -----------------------------

def test_succeeds_without_retry():
    monkeypatched_sleeps: list[float] = []
    result, attempts = run_with_resilience(OkTool(), {}, timeout=1.0, max_retries=3,
                                           sleep=monkeypatched_sleeps.append)
    assert result == "成功"
    assert attempts == 1
    assert monkeypatched_sleeps == []  # 首次成功不应有任何退避


def test_retries_with_exponential_backoff_then_gives_up():
    """反例：可重试失败应退避重试至上限，退避间隔递增且次数等于 max_retries。"""
    monkeypatch_sleeps: list[float] = []
    tool = FailTool()
    monkeypatched_settings_backup = (settings.RETRY_BASE_DELAY_SECONDS, settings.RETRY_JITTER)
    settings.RETRY_BASE_DELAY_SECONDS = 0.1
    settings.RETRY_JITTER = 0.0
    try:
        with pytest.raises(ToolError):
            run_with_resilience(tool, {}, timeout=1.0, max_retries=3,
                                sleep=monkeypatch_sleeps.append)
    finally:
        settings.RETRY_BASE_DELAY_SECONDS, settings.RETRY_JITTER = monkeypatched_settings_backup

    assert len(tool.calls) == 3          # 重试到上限
    assert len(monkeypatch_sleeps) == 2  # 3 次尝试 -> 2 次退避
    assert monkeypatch_sleeps[1] > monkeypatch_sleeps[0]  # 指数递增


def test_fatal_error_short_circuits_without_retry():
    """反例：不可重试错误（如参数非法）必须立即短路，不浪费重试预算。"""
    monkeypatched_sleeps: list[float] = []
    tool = FailTool(error=ToolError("参数非法", retryable=False, kind=ToolErrorKind.FATAL))
    with pytest.raises(ToolError):
        run_with_resilience(tool, {}, timeout=1.0, max_retries=5,
                            sleep=monkeypatched_sleeps.append)
    assert len(tool.calls) == 1      # 只调用一次
    assert monkeypatched_sleeps == []  # 无退避


def test_backoff_delay_exponential_and_capped(monkeypatch):
    """退避曲线：指数增长并封顶。"""
    monkeypatch.setattr(settings, "RETRY_BASE_DELAY_SECONDS", 0.1)
    monkeypatch.setattr(settings, "RETRY_MAX_DELAY_SECONDS", 1.0)
    monkeypatch.setattr(settings, "RETRY_JITTER", 0.0)
    assert abs(backoff_delay(1) - 0.1) < 1e-9
    assert abs(backoff_delay(2) - 0.2) < 1e-9
    assert abs(backoff_delay(3) - 0.4) < 1e-9
    assert abs(backoff_delay(10) - 1.0) < 1e-9  # 封顶


# ----------------------------- 熔断 -----------------------------

def test_breaker_opens_after_threshold_and_fails_fast():
    """反例：连续失败达阈值后熔断器开路，后续调用不再打下游（快速失败）。"""
    breaker = CircuitBreaker(failure_threshold=2, recovery_seconds=60.0)
    tool = FailTool()
    # 触发两次失败 -> 开路
    for _ in range(2):
        with pytest.raises(ToolError):
            run_with_resilience(tool, {}, timeout=1.0, max_retries=1, breaker=breaker,
                                sleep=lambda _s: None)
    assert breaker.state is BreakerState.OPEN
    calls_before = len(tool.calls)

    # 开路状态下调用：应快速失败，且底层工具一次都不该被调用
    with pytest.raises(CircuitBreakerOpen):
        run_with_resilience(tool, {}, timeout=1.0, max_retries=3, breaker=breaker,
                            sleep=lambda _s: None)
    assert len(tool.calls) == calls_before


def test_breaker_recovers_to_half_open_then_closes():
    """熔断开路冷却满 -> 半开放行一次；成功 -> 闭合。"""
    breaker = CircuitBreaker(failure_threshold=1, recovery_seconds=0.1)
    fail = FailTool()
    with pytest.raises(ToolError):
        run_with_resilience(fail, {}, timeout=1.0, max_retries=1, breaker=breaker,
                            sleep=lambda _s: None)
    assert breaker.state is BreakerState.OPEN

    time.sleep(0.15)  # 等过冷却期
    result, attempts = run_with_resilience(OkTool(), {}, timeout=1.0, breaker=breaker,
                                           max_retries=2, sleep=lambda _s: None)
    assert result == "成功"
    assert attempts == 1
    assert breaker.state is BreakerState.CLOSED  # 半开试探成功后闭合


# ----------------------------- 默认工具 -----------------------------

def test_supplier_order_tool_can_force_result():
    """默认工具可通过注入 rng 控制行为，保证测试确定性。"""
    ok = SupplierOrderTool(latency_seconds=0.0, failure_rate=0.0, rng=lambda: 0.99)
    assert ok.call({}) == "供应商下单成功"

    fail = SupplierOrderTool(latency_seconds=0.0, failure_rate=1.0, rng=lambda: 0.01)
    with pytest.raises(ToolError) as ei:
        fail.call({})
    assert ei.value.kind is ToolErrorKind.RETRYABLE
