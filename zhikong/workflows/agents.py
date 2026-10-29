"""工作流节点（Agent）与路由函数。

四大专职节点：analyst（RAG 风险分析）/ approval_gate（Human-in-the-Loop）/ executor（执行+重试）/ monitor（汇总）。
关键企业级升级点：
- approval_gate 使用条件中断 langgraph.types.interrupt，仅在需要时暂停人工审批，
  避免对「低风险自动执行」与「重试循环」产生误暂停（教材版 interrupt_before 会误暂停每次重试）。
- 每个节点均向审计子系统埋点，全链路留痕。
"""
from __future__ import annotations

import random
import time
from typing import Optional

from langgraph.types import interrupt

from core.config import settings
from core.logging import logger
from services import idempotency_service as idem
from services.audit_service import EventType, append
from services.knowledge_service import search as kg_search
from workflows.providers import LLMProvider, build_llm, enforce_risk_floor
from workflows.state import WorkflowState
from workflows.tools import (
    CircuitBreaker,
    SupplierOrderTool,
    ToolError,
    backoff_delay,
    run_with_resilience,
)

_MAX_DETAIL_LEN = 2000


def _safe_detail(detail: str) -> str:
    """约束不可信输入长度，避免超长内容撑爆上下文/成本。"""
    if not isinstance(detail, str):
        detail = str(detail)
    detail = detail.strip()
    return detail[:_MAX_DETAIL_LEN]


def _backoff_delay(retry: int) -> float:
    """指数退避 + 抖动：retry 为已失败次数（>=1 时在下次重试前调用）。

    实现已下沉到 `workflows.tools.backoff_delay`（P1-1：重试集中在工具层），
    此处保留薄封装以兼容既有调用与测试。
    """
    return backoff_delay(retry)


# ----------------------------- 工具与熔断器（P1-1） -----------------------------

_TOOL: Optional[Any] = None
_BREAKER: Optional[CircuitBreaker] = None


def _tool() -> Any:
    """默认外部工具（供应商下单）。测试可 monkeypatch `agents._TOOL` 注入替身。"""
    global _TOOL
    if _TOOL is None:
        _TOOL = SupplierOrderTool()
    return _TOOL


def _breaker() -> CircuitBreaker:
    """进程内熔断器单例。测试可 monkeypatch `agents._BREAKER` 注入替身。"""
    global _BREAKER
    if _BREAKER is None:
        _BREAKER = CircuitBreaker(
            failure_threshold=settings.BREAKER_FAILURE_THRESHOLD,
            recovery_seconds=settings.BREAKER_RECOVERY_SECONDS,
        )
    return _BREAKER

_LLM: Optional[LLMProvider] = None


def _llm() -> LLMProvider:
    global _LLM
    if _LLM is None:
        _LLM = build_llm()
    return _LLM


# ----------------------------- Analyst -----------------------------

def analyst_node(state: WorkflowState) -> dict:
    tid = state.get("thread_id")
    trace = state.get("trace_id")

    # 1) RAG 检索企业规章制度（真实向量检索）；输入做长度约束
    amount = float(state["amount"])
    detail = _safe_detail(state.get("request_detail", ""))
    query = f"{state['request_type']} {detail} 金额{amount} {state.get('department', '')}"
    knowledge = kg_search(query)

    # 2) 基于检索规则做风险评估（可插拔：规则兜底 / LLM）
    result = _llm().analyze(
        request_type=state["request_type"],
        detail=detail,
        amount=amount,
        department=state.get("department", ""),
        knowledge=knowledge,
    )

    # 3) 金额阈值硬下限：无论模型/注入如何输出，大额不可被降级为低风险
    floored = enforce_risk_floor(result.risk_level, amount)
    if floored != result.risk_level:
        logger.warning(f"[analyst] thread={tid} 风险被金额阈值强制提升 "
                       f"{result.risk_level}->{floored}")
        result.reason = f"[金额阈值强制提升 {result.risk_level}->{floored}] {result.reason}"
        result.risk_level = floored

    need_approval = (
        amount > settings.AUTO_APPROVE_THRESHOLD
        or result.risk_level in ("medium", "high")
    )
    current_phase = "execute" if not need_approval else "approve"
    approval_status = "approved" if not need_approval else "pending"

    analysis_report = (
        f"风险等级: {result.risk_level}\n"
        f"评估理由: {result.reason}\n"
        f"建议审批人: {', '.join(result.required_approvers) or '无'}\n"
        f"模型: {result.model}"
    )

    append(EventType.RISK_ANALYZED, actor="analyst", actor_role="system",
           thread_id=tid, trace_id=trace, node="analyst",
           phase_before=state.get("current_phase"),
           phase_after=current_phase,
           detail={"risk_level": result.risk_level, "need_approval": need_approval,
                   "model": result.model, "approvers": result.required_approvers})

    logger.info(f"[analyst] thread={tid} risk={result.risk_level} need_approval={need_approval}")

    return {
        "risk_level": result.risk_level,
        "knowledge_context": knowledge,
        "analysis_report": analysis_report,
        "required_approvers": result.required_approvers,
        "current_phase": current_phase,
        "approval_status": approval_status,
        "logs": [f"[分析Agent] 风险:{result.risk_level} | {'需审批' if need_approval else '自动放行'}({result.model})"],
    }


# --------------------------- Approval Gate ---------------------------

def approval_gate_node(state: WorkflowState) -> dict:
    """人工审批闸门：仅在需要审批时暂停（条件中断）。

    使用 langgraph.types.interrupt + Command(resume=...) 实现 Human-in-the-Loop。
    首次进入时 interrupt() 暂停；恢复时 interrupt() 返回外部传入的审批结论（dict）。
    """
    tid = state.get("thread_id")
    trace = state.get("trace_id")

    decision = interrupt({
        "type": "approval_required",
        "thread_id": tid,
        "risk_level": state.get("risk_level"),
        "amount": state.get("amount"),
        "analysis_report": state.get("analysis_report"),
    })

    # decision 为 Command(resume=...) 传入的审批结论
    approved = bool(decision.get("approved", False)) if isinstance(decision, dict) else True
    comment = (decision or {}).get("comment", "") if isinstance(decision, dict) else ""
    if not approved:
        logger.info(f"[approval] thread={tid} 已驳回")
        return {"approval_status": "rejected", "current_phase": "done",
                "logs": [f"[审批Agent] ❌ 已驳回 {('原因:' + comment) if comment else ''}"]}
    logger.info(f"[approval] thread={tid} 已批准")
    return {"approval_status": "approved", "current_phase": "execute",
            "logs": [f"[审批Agent] ✅ 已批准 {('备注:' + comment) if comment else ''}"]}


# ----------------------------- Executor -----------------------------

def executor_node(state: WorkflowState) -> dict:
    tid = state.get("thread_id")
    trace = state.get("trace_id")
    retry = int(state.get("retry_count", 0))
    idem_key = state.get("idempotency_key") or tid

    # 幂等命中：同一请求已成功执行过 -> 直接返回缓存结果，绝不重复副作用
    cached = idem.get(idem_key) if idem_key else None
    if cached and cached.get("status") == "success":
        append(EventType.EXEC_RESULT, actor="executor", actor_role="system",
               thread_id=tid, trace_id=trace, node="executor", phase_after="done",
               detail={"result": "idempotent_hit", "key": idem_key})
        logger.info(f"[executor] thread={tid} 幂等命中，跳过重复执行 key={idem_key[:8]}")
        return {
            "execution_result": "success",
            "final_report": cached.get("payload") or state.get("final_report", ""),
            "current_phase": "done",
            "logs": [f"[执行Agent] ♻ 幂等命中，未重复执行（key={idem_key[:8]}）"],
        }

    # 执行前占坑：以 pending 状态登记，关闭「执行成功→崩溃→重放再次执行」窗口。
    # 若此前已 pending（上次执行到一半崩溃），claim_pending 返回 False，仍安全重放。
    idem.claim_pending(idem_key, tid)

    # 指数退避 + 抖动：非首次尝试前先等待，避免失败后立即重入打爆下游
    if retry > 0:
        delay = _backoff_delay(retry)
        logger.info(f"[executor] thread={tid} 退避 {delay:.2f}s 后重试（第 {retry + 1} 次）")
        time.sleep(delay)

    append(EventType.EXEC_START, actor="executor", actor_role="system",
           thread_id=tid, trace_id=trace, node="executor",
           detail={"attempt": retry + 1, "max_tool_retries": settings.TOOL_MAX_RETRIES,
                   "timeout": settings.TOOL_TIMEOUT_SECONDS})
    logger.info(f"[executor] thread={tid} 第 {retry + 1} 次执行")

    try:
        # P1-1：调用真实工具，由工具层负责超时 + 指数退避重试 + 熔断（不再用 random 模拟）
        payload = {
            "request_type": state.get("request_type"),
            "request_detail": state.get("request_detail"),
            "amount": state.get("amount"),
            "department": state.get("department"),
            "idempotency_key": idem_key,
        }
        _outcome, attempts = run_with_resilience(_tool(), payload, breaker=_breaker())
        tool_name = getattr(_tool(), "name", "unknown")
        report = (
            "执行报告(企业版)\n"
            f"- 事项: {state['request_detail']}\n"
            f"- 金额: ¥{state['amount']:,.2f}\n"
            f"- 部门: {state.get('department','')}\n"
            f"- 工具: {tool_name}（第 {attempts} 次尝试成功）\n"
            "- 状态: 已完成（已写入审计）"
        )
        # 先落地台账为 success，再写审计：确保崩溃恢复时台账已是最终态，
        # 避免「执行完成但未记账」导致重放重复副作用。
        idem.update_status(idem_key, "success", "success", report)
        append(EventType.EXEC_RESULT, actor="executor", actor_role="system",
               thread_id=tid, trace_id=trace, node="executor",
               phase_after="done",
               detail={"result": "success", "attempts": attempts, "tool": tool_name})
        return {
            "execution_result": "success",
            "final_report": report,
            "current_phase": "done",
            "logs": [f"[执行Agent] ✅ 调用工具 '{tool_name}' 成功（第{attempts}次尝试）"],
        }
    except ToolError as e:
        # 工具层已耗尽重试 / 不可重试 / 熔断开路 -> 终态失败（图级不再自循环重试）
        kind = getattr(e, "kind", None)
        append(EventType.EXEC_RESULT, actor="executor", actor_role="system",
               thread_id=tid, trace_id=trace, node="executor", level="ERROR",
               detail={"result": "failed", "error": str(e),
                       "kind": kind.value if hasattr(kind, "value") else None})
        return {
            "execution_result": "failed",
            "retry_count": retry + 1,
            "error": str(e),
            "logs": [f"[执行Agent] ❌ 工具调用失败: {e}"],
        }
    except Exception as e:  # pragma: no cover
        append(EventType.EXEC_RESULT, actor="executor", actor_role="system",
               thread_id=tid, trace_id=trace, node="executor", level="ERROR",
               detail={"result": "error", "error": str(e)})
        return {
            "execution_result": "error",
            "retry_count": retry + 1,
            "error": str(e),
            "logs": [f"[执行Agent] ❌ 异常: {e}"],
        }


# ----------------------------- Monitor -----------------------------

def monitor_node(state: WorkflowState) -> dict:
    tid = state.get("thread_id")
    trace = state.get("trace_id")
    status = "成功" if state.get("execution_result") == "success" else "失败/取消"
    append(EventType.WORKFLOW_DONE, actor="system", actor_role="system",
           thread_id=tid, trace_id=trace, node="monitor",
           phase_after="done",
           detail={"final_status": status, "retry_count": state.get("retry_count", 0),
                   "execution_result": state.get("execution_result")})
    logger.info(f"[monitor] thread={tid} 流程结束 | {status}")
    return {"current_phase": "done",
            "logs": [f"[监控] 流程结束 | 状态:{status} | 重试:{state.get('retry_count', 0)}"]}


# ----------------------------- 路由函数 -----------------------------

def route_after_analysis(state: WorkflowState) -> str:
    # 低风险/自动放行 -> 直接执行；否则进入人工审批闸门
    return "executor" if state.get("current_phase") == "execute" else "approval_gate"


def route_after_approval_gate(state: WorkflowState) -> str:
    return "executor" if state.get("approval_status") == "approved" else "monitor"


def route_after_execution(state: WorkflowState) -> str:
    # P1-1：技术性重试已下沉到工具层（workflows.tools.run_with_resilience），
    # 图级不再自循环重试，避免"图级重试 × 工具层重试"叠加放大调用次数；
    # 此处只负责终态收敛，成败都交由 monitor 汇总。
    return "monitor"
