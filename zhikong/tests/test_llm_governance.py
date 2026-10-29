"""P1-4：LLM 调用治理专项测试（超时分类 / 退避重试 / 并发限流 / 降级 / 指标）。"""
from __future__ import annotations

import threading
import time

import pytest

from core.metrics import REGISTRY, record_llm_call
from workflows.llm_gateway import LLMGateway
from workflows.providers import AnalysisResult, LLMError, LLMProvider


class FlakyLLM(LLMProvider):
    """按设定抛错的替身，记录实际调用次数。"""

    def __init__(self, retryable: bool = True, hold: float = 0.0) -> None:
        self.calls = 0
        self.retryable = retryable
        self.hold = hold

    @property
    def name(self) -> str:
        return "flaky"

    def analyze(self, **kwargs) -> AnalysisResult:
        self.calls += 1
        if self.hold:
            time.sleep(self.hold)
        raise LLMError("模拟模型故障", retryable=self.retryable)


class OkLLM(LLMProvider):
    @property
    def name(self) -> str:
        return "ok"

    def analyze(self, **kwargs) -> AnalysisResult:
        return AnalysisResult(risk_level="low", reason="ok", required_approvers=[], model="ok")


def _kw(amount: float = 100.0) -> dict:
    return {"request_type": "procurement", "detail": "采购", "amount": amount,
            "department": "技术部", "knowledge": ""}


def _no_sleep(_: float) -> None:
    """测试中去退避等待，保持用例毫秒级。"""


def test_retryable_error_retries_then_falls_back():
    """可重试错误：耗尽重试预算后才降级。"""
    flaky = FlakyLLM(retryable=True)
    gw = LLMGateway(flaky, fallback=OkLLM(), max_retries=2, sleep=_no_sleep)
    res = gw.analyze(**_kw())
    assert flaky.calls == 3, "首次 + 2 次重试"
    assert res.model == "ok", "重试耗尽应降级到 fallback"


def test_non_retryable_error_short_circuits():
    """不可重试错误（鉴权/参数/响应格式）：立即短路，不消耗重试预算。"""
    flaky = FlakyLLM(retryable=False)
    gw = LLMGateway(flaky, fallback=OkLLM(), max_retries=3, sleep=_no_sleep)
    gw.analyze(**_kw())
    assert flaky.calls == 1, "不可重试错误不得重试"


def test_fallback_respects_amount_floor():
    """降级到规则模型后，金额下限仍然生效（不能被降级绕过）。"""
    flaky = FlakyLLM(retryable=False)
    gw = LLMGateway(flaky, max_retries=0, sleep=_no_sleep)  # 未传 fallback -> 用规则模型
    res = gw.analyze(**_kw(amount=300000.0))
    assert res.risk_level == "high", "大额即便降级也必须 high"


def test_concurrency_limit_throttles_instead_of_queueing_forever():
    """并发额度耗尽时：有限排队 + throttled 降级，而非无限等待。"""
    slow = FlakyLLM(retryable=False, hold=0.5)
    gw = LLMGateway(slow, fallback=OkLLM(), max_concurrency=1, queue_timeout=0.05,
                    max_retries=0, sleep=_no_sleep)

    # 主线程先占住唯一额度，另一个线程再来时应排队超时并降级
    assert gw._sem.acquire(timeout=0.1)
    result_box: dict[str, AnalysisResult] = {}

    def worker() -> None:
        result_box["res"] = gw.analyze(**_kw())

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=3)
    gw._sem.release()

    assert not t.is_alive(), "限流分支不应挂起线程"
    assert result_box["res"].model == "ok", "排队超时应降级到 fallback"


def test_fallback_disabled_raises():
    """关闭降级时，模型失败必须显式抛错（结论不能凭空产生）。"""
    from core.config import settings

    gw = LLMGateway(FlakyLLM(retryable=False), fallback=None, max_retries=0, sleep=_no_sleep)
    old = settings.LLM_FALLBACK_ENABLED
    settings.LLM_FALLBACK_ENABLED = False
    try:
        with pytest.raises(LLMError):
            gw.analyze(**_kw())
    finally:
        settings.LLM_FALLBACK_ENABLED = old


def test_metrics_record_outcomes():
    """治理层必须可观测：成功/降级/限流都要能被指标区分。"""
    before = _counter("zhikong_llm_calls_total", {"provider": "metric-test", "outcome": "success"})
    record_llm_call("metric-test", "success", 0.01)
    after = _counter("zhikong_llm_calls_total", {"provider": "metric-test", "outcome": "success"})
    assert after == before + 1


def _counter(name: str, labels: dict) -> float:
    for metric in REGISTRY.collect():
        for sample in metric.samples:
            if sample.name == name and sample.labels == labels:
                return float(sample.value)
    return 0.0
