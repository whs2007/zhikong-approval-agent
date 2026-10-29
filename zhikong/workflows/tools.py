"""外部工具调用韧性层（P1-1）：超时 + 指数退避重试 + 熔断 + 错误分类。

背景（复验 R3-1 / B1 短板）
--------------------------
原 `executor_node` 用 `random.random() > 0.4` 模拟外部调用，接真实"下单/发消息"API 后
既无超时、也无退避重试、更无熔断——失败会变成忙等风暴，并把已经不可用的下游继续打爆。
本模块把"一次外部调用"收敛为统一入口 `run_with_resilience()`，并提供明确的错误分类：

- **超时**：Python 无法强制中断阻塞调用，用 `ThreadPoolExecutor + future.result(timeout)`
  做硬兜底；真实 Tool 应自身支持 timeout（如 `requests.post(..., timeout=...)`）以释放连接。
- **退避**：指数 + 抖动 + 封顶（复用 `settings.RETRY_*`），抖动避免多实例重试对齐形成风暴。
- **熔断**：连续失败达阈值即开路，冷却期内快速失败；冷却满进入半开试探一次，成功即闭合。
- **错误分类**：`ToolError.retryable=False`（如参数非法、鉴权失败）立即短路，不浪费重试预算。

为什么重试下沉到工具层（而不是继续放在图级）
-----------------------------------------
图级重试会把"工具超时"放大为整个执行语义的重跑（重复副作用、幂等压力翻倍），且重试次数
在两层叠加后难以预估。此处让**工具层承担技术性重试**（超时/网络抖动），
图级只负责编排与终态收敛，重试次数单一可控。
"""
from __future__ import annotations

import random
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional, Protocol

from core.config import settings
from core.logging import logger

__all__ = [
    "ToolErrorKind",
    "ToolError",
    "CircuitBreakerOpen",
    "BreakerState",
    "CircuitBreaker",
    "Tool",
    "SupplierOrderTool",
    "backoff_delay",
    "call_with_timeout",
    "run_with_resilience",
]


class ToolErrorKind(str, Enum):
    """工具错误的语义分类，决定重试策略与观测口径。"""

    TIMEOUT = "timeout"      # 超时（可重试）
    RETRYABLE = "retryable"  # 瞬时故障：网络抖动/5xx（可重试）
    FATAL = "fatal"          # 致命：参数非法/鉴权失败/熔断（不可重试）


class ToolError(Exception):
    """工具调用失败。

    - `retryable=True`：超时等瞬时故障，退避重试有意义。
    - `retryable=False`：致命错误，重试无意义，立即短路避免浪费预算。
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        kind: ToolErrorKind = ToolErrorKind.RETRYABLE,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.kind = kind


class CircuitBreakerOpen(ToolError):
    """熔断器处于开路：下游已被判定不可用，快速失败以保护调用方与下游双方。"""

    def __init__(self, message: str = "熔断器已开路") -> None:
        super().__init__(message, retryable=False, kind=ToolErrorKind.FATAL)


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    """连续失败计数熔断器（进程内）。

    单实例部署适用；多实例需改为 Redis/共享存储实现跨实例状态共享。
    """

    failure_threshold: int = 3
    recovery_seconds: float = 30.0
    state: BreakerState = field(default=BreakerState.CLOSED, init=False)
    failures: int = field(default=0, init=False)
    opened_at: float = field(default=0.0, init=False)

    def check(self) -> None:
        """调用前检查；开路且未过冷却期则抛 `CircuitBreakerOpen`（快速失败）。"""
        if self.state is not BreakerState.OPEN:
            return
        elapsed = time.monotonic() - self.opened_at
        if elapsed >= self.recovery_seconds:
            # 冷却期满 -> 半开，放行一次试探调用
            self.state = BreakerState.HALF_OPEN
            logger.info("[breaker] 冷却期满，进入半开状态，放行一次试探调用")
            return
        remaining = self.recovery_seconds - elapsed
        raise CircuitBreakerOpen(f"熔断器开路，剩余恢复时间 {remaining:.1f}s")

    def record_success(self) -> None:
        if self.state is not BreakerState.CLOSED:
            logger.info("[breaker] 调用成功，熔断器关闭")
        self.failures = 0
        self.state = BreakerState.CLOSED

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.failure_threshold and self.state is not BreakerState.OPEN:
            self.state = BreakerState.OPEN
            self.opened_at = time.monotonic()
            logger.warning(f"[breaker] 连续失败 {self.failures} 次，熔断器开路")


# 共享线程池：每次调用新建线程池开销过大，进程生命周期内复用。
_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="tool-call")


def backoff_delay(retry: int) -> float:
    """指数退避 + 抖动（迁移自原 `agents._backoff_delay`，供工具层复用）。

    `retry` 为已失败次数（>=1）。抖动避免多实例/多线程重试对齐形成同步风暴。
    """
    base = max(0.0, settings.RETRY_BASE_DELAY_SECONDS)
    cap = max(base, settings.RETRY_MAX_DELAY_SECONDS)
    raw = min(base * (2 ** max(retry - 1, 0)), cap)
    jitter = min(max(settings.RETRY_JITTER, 0.0), 1.0)
    return max(0.0, raw * (1 + random.uniform(-jitter, jitter)))


class Tool(Protocol):
    """工具契约：接收结构化 payload，返回业务结果字符串。

    真实实现应支持 `timeout` 参数（如 `requests.post(..., timeout=timeout)`），
    以便超时时能真正释放底层连接，而不是仅靠外层线程兜底。
    """

    name: str

    def call(self, payload: dict, timeout: float | None = None) -> str: ...


class SupplierOrderTool:
    """默认供应商下单工具：模拟网络延迟与瞬时失败。

    接入真实业务系统时，实现同名 `call(payload, timeout)` 替换即可；
    调用侧已由幂等台账保证同一 key 不重复下单。
    """

    name = "supplier_order"

    def __init__(
        self,
        latency_seconds: float = 0.02,
        failure_rate: float | None = None,
        rng: Optional[Callable[[], float]] = None,
    ) -> None:
        # failure_rate 未显式传入时跟随 settings.TOOL_FAILURE_RATE
        # （便于不同环境/测试统一调节，测试环境由 conftest 置 0 保证确定性）
        self.latency_seconds = latency_seconds
        self.failure_rate = settings.TOOL_FAILURE_RATE if failure_rate is None else failure_rate
        self._rng = rng or random.random

    def call(self, payload: dict, timeout: float | None = None) -> str:
        # 模拟网络往返；真实场景替换为 requests/OpenSDK 调用并传入 timeout
        time.sleep(self.latency_seconds * (1 + self._rng()))
        if self._rng() < self.failure_rate:
            raise ToolError("供应商 API 超时", retryable=True, kind=ToolErrorKind.RETRYABLE)
        return "供应商下单成功"


def call_with_timeout(tool: Tool, payload: dict, timeout: float) -> str:
    """单次调用并施加超时；超时转为 `ToolError(retryable=True, kind=TIMEOUT)`。"""
    fut: Future = _EXECUTOR.submit(tool.call, payload, timeout)
    try:
        return fut.result(timeout=timeout)
    except FuturesTimeoutError as e:
        # 注意：仅能中断等待，底层线程仍可能在跑；因此真实 Tool 必须自身带 timeout
        raise ToolError(
            f"调用工具 {getattr(tool, 'name', 'unknown')} 超时（>{timeout}s）",
            retryable=True,
            kind=ToolErrorKind.TIMEOUT,
        ) from e


def run_with_resilience(
    tool: Tool,
    payload: dict,
    *,
    timeout: float | None = None,
    max_retries: int | None = None,
    breaker: CircuitBreaker | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[str, int]:
    """带韧性地执行一次工具调用：超时 + 指数退避重试 + 熔断。

    返回 `(结果, 实际尝试次数)`；全部失败时抛出最后的 `ToolError`。
    """
    timeout = timeout if timeout is not None else settings.TOOL_TIMEOUT_SECONDS
    max_retries = max_retries if max_retries is not None else settings.TOOL_MAX_RETRIES
    attempts = 0
    last_err: ToolError | None = None

    while True:
        attempts += 1
        if breaker is not None:
            # 开路且未过冷却期 -> 直接快速失败，不再打下游
            breaker.check()
        try:
            result = call_with_timeout(tool, payload, timeout)
        except CircuitBreakerOpen:
            raise
        except ToolError as e:
            last_err = e
        except Exception as e:
            # 非预期异常按致命处理：不重试（避免放大 bug），但保留上下文便于排查
            last_err = ToolError(
                f"工具 {getattr(tool, 'name', 'unknown')} 内部异常: {e}",
                retryable=False,
                kind=ToolErrorKind.FATAL,
            )
        else:
            if breaker is not None:
                breaker.record_success()
            return result, attempts

        if breaker is not None:
            breaker.record_failure()

        if last_err is not None and not last_err.retryable:
            logger.warning(f"[tool] 不可重试错误，短路返回: {last_err}")
            raise last_err
        if attempts >= max_retries:
            logger.warning(f"[tool] 重试 {attempts} 次后仍失败: {last_err}")
            raise last_err

        delay = backoff_delay(attempts)
        logger.info(f"[tool] 第 {attempts} 次失败，退避 {delay:.2f}s 后重试")
        sleep(delay)
