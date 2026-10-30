"""LLM 调用治理层（P1-4）。

把"模型调用"当作**不可靠的远程依赖**来治理，而不是裸 try/except：

1. **并发限流**：信号量限制同时占用模型的调用数，避免打满额度/连接被限流；
   排队超时记为 `throttled` 并降级，而不是无限排队拖垮请求。
2. **指数退避重试**：仅对可重试错误（超时/网络/5xx/429）重试，且带抖动；
   鉴权失败、参数非法、响应不可解析等**不可重试**错误立即短路，不浪费重试预算。
3. **降级（fallback）**：重试耗尽后回退规则模型，保证工作流不因模型抖动而中断；
   可通过 `LLM_FALLBACK_ENABLED=0` 关闭（用于"结论必须来自模型"的场景）。
4. **可观测**：每次调用记录 `provider/outcome` 与耗时，并暴露并发占用 Gauge。

设计要点：provider 自身**不静默降级**（见 OpenAILLM.analyze），否则上层无法
区分"调用失败"与"模型结论"，重试与指标都会失真。
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable

from core.config import settings
from core.logging import logger
from core.metrics import LLM_INFLIGHT, record_llm_call, record_llm_tokens
from workflows.providers import (
    AnalysisResult,
    LLMError,
    LLMProvider,
    RuleBasedLLM,
)
from workflows.tools import backoff_delay


class LLMGateway(LLMProvider):
    """给任意 LLMProvider 套上超时/重试/限流/降级的治理外壳。"""

    def __init__(
        self,
        primary: LLMProvider,
        fallback: LLMProvider | None = None,
        *,
        max_concurrency: int | None = None,
        queue_timeout: float | None = None,
        max_retries: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._max_concurrency = max(1, max_concurrency or settings.LLM_MAX_CONCURRENCY)
        self._queue_timeout = queue_timeout if queue_timeout is not None else settings.LLM_QUEUE_TIMEOUT_SECONDS
        self._max_retries = max(0, max_retries if max_retries is not None else settings.LLM_MAX_RETRIES)
        self._sleep = sleep
        self._sem = threading.Semaphore(self._max_concurrency)
        self._name = getattr(primary, "name", primary.__class__.__name__)

    @property
    def name(self) -> str:
        return self._name

    def _resolve_fallback(self) -> LLMProvider | None:
        if self._fallback is not None:
            return self._fallback
        return RuleBasedLLM() if settings.LLM_FALLBACK_ENABLED else None

    def analyze(self, *, request_type: str, detail: str, amount: float,
                department: str, knowledge: str) -> AnalysisResult:
        started = time.perf_counter()
        attempts = 0
        last_err: LLMError | None = None

        while True:
            attempts += 1
            # 1) 限流：排队而非无限等待，超时记为 throttled
            if not self._sem.acquire(timeout=self._queue_timeout):
                logger.warning(
                    f"[llm] 并发占满，排队 {self._queue_timeout}s 未获得额度（上限 "
                    f"{self._max_concurrency}），本次降级"
                )
                record_llm_call(self._name, "throttled", time.perf_counter() - started)
                return self._degrade(request_type, detail, amount, department, knowledge,
                                     "并发额度耗尽")

            try:
                LLM_INFLIGHT.inc()
                try:
                    result = self._primary.analyze(
                        request_type=request_type, detail=detail, amount=amount,
                        department=department, knowledge=knowledge,
                    )
                finally:
                    LLM_INFLIGHT.dec()
                    self._sem.release()
                record_llm_call(self._name, "success", time.perf_counter() - started)
                record_llm_tokens(
                    self._name,
                    prompt_tokens=result.prompt_tokens,
                    completion_tokens=result.completion_tokens,
                )
                return result
            except LLMError as e:
                last_err = e
                if not e.retryable:
                    logger.warning(f"[llm] 不可重试错误，直接降级: {e}")
                    break
                if attempts > self._max_retries:
                    logger.warning(f"[llm] 已重试 {self._max_retries} 次仍失败: {e}")
                    break
                delay = backoff_delay(attempts)
                logger.info(f"[llm] 第 {attempts} 次失败，{delay:.2f}s 后重试: {e}")
                self._sleep(delay)
            except Exception as e:  # 非预期异常：不重试（避免放大 bug），直接降级
                last_err = LLMError(f"LLM 内部异常: {e}", retryable=False)
                logger.error(f"[llm] 内部异常，直接降级: {e}")
                break

        # 2) 重试耗尽 / 不可重试 -> 降级
        record_llm_call(self._name, "fallback", time.perf_counter() - started)
        return self._degrade(request_type, detail, amount, department, knowledge, str(last_err))

    def _degrade(self, request_type: str, detail: str, amount: float,
                 department: str, knowledge: str, reason: str) -> AnalysisResult:
        fb = self._resolve_fallback()
        if fb is None:
            record_llm_call(self._name, "failed", 0.0)
            raise LLMError(f"LLM 调用失败且已关闭降级: {reason}", retryable=False)
        return fb.analyze(
            request_type=request_type, detail=detail, amount=amount,
            department=department, knowledge=knowledge,
        )
