"""工作流服务层：封装 LangGraph app 的提交/暂停/恢复/查询。

- 同步 invoke（SqliteSaver 为同步 Checkpointer），在异步 API 中经 asyncio.to_thread 调用。
- submit：首次 invoke；若低风险提示自动放行则一次跑完；若进入审批闸门则暂停返回待审批。
- decide：审批人决策 -> update_state -> resume，并记录真实操作者审计。
"""
from __future__ import annotations

import asyncio
import time
import uuid

from langgraph.types import Command

from core.logging import bind_trace, get_trace_id, reset_trace
from core.metrics import (
    observe_invoke_duration,
    record_workflow_final,
    record_workflow_submission,
)
from services.audit_service import EventType, append, new_trace_id
from workflows.graph import build_workflow


class WorkflowService:
    def __init__(self) -> None:
        self.app = build_workflow()

    # ----------------------- 同步核心（供 to_thread 调用） -----------------------

    def _submit_sync(self, state: dict, actor: str, actor_role: str) -> dict:
        thread_id = state["thread_id"]
        config = {"configurable": {"thread_id": thread_id}}
        # P1-2：把链路 ID 绑定到当前上下文（to_thread 会传播 contextvars），
        # 使 analyst/executor/monitor 等节点的日志自动携带 trace_id，无需手动拼接。
        tokens = bind_trace(trace_id=state.get("trace_id", ""), thread_id=thread_id)
        try:
            append(EventType.WORKFLOW_SUBMIT, actor=actor, actor_role=actor_role,
                   thread_id=thread_id, trace_id=state.get("trace_id"),
                   detail={"request_type": state["request_type"], "amount": state["amount"],
                           "department": state.get("department")})
            started = time.perf_counter()
            result = self.app.invoke(state, config)
            observe_invoke_duration(time.perf_counter() - started)
            snap = self.app.get_state(config)
            paused = bool(snap.next)
            # P1-3：按初态与终态埋点，Prometheus 侧可算成功率与待审批比例
            record_workflow_submission("approve" if paused else "execute")
            record_workflow_final(_final_result(result, paused))
            if paused:
                # 进入人工审批闸门，登记暂停审计
                append(EventType.APPROVAL_PAUSED, actor="system", actor_role="system",
                       thread_id=thread_id, trace_id=state.get("trace_id"),
                       node="approval_gate",
                       detail={"risk_level": result.get("risk_level"),
                               "amount": state.get("amount")})
            return {"state": _public_state(result), "paused": paused,
                    "next": list(snap.next) if snap.next else []}
        finally:
            reset_trace(*tokens)

    def _decide_sync(self, thread_id: str, approved: bool, comment: str,
                     actor: str, actor_role: str) -> dict:
        config = {"configurable": {"thread_id": thread_id}}
        # P1-2：恢复审批时同样绑定链路（trace_id 从现有 checkpoint 状态取回）
        snapshot = self.app.get_state(config)
        trace_id = (snapshot.values or {}).get("trace_id", "") if snapshot else ""
        tokens = bind_trace(trace_id=trace_id, thread_id=thread_id)
        try:
            # 用 Command(resume=...) 将审批结论传入 interrupt() 恢复执行
            result = self.app.invoke(
                Command(resume={"approved": approved, "comment": comment,
                                "actor": actor, "actor_role": actor_role}),
                config,
            )
            append(EventType.APPROVAL_DECISION, actor=actor, actor_role=actor_role,
                   thread_id=thread_id, trace_id=trace_id,
                   detail={"decision": "approved" if approved else "rejected",
                           "comment": comment})
            return _public_state(result)
        finally:
            reset_trace(*tokens)

    def _status_sync(self, thread_id: str) -> dict | None:
        config = {"configurable": {"thread_id": thread_id}}
        snap = self.app.get_state(config)
        if snap is None or snap.values is None:
            return None
        values = _public_state(snap.values)
        values["paused"] = bool(snap.next)
        values["next"] = list(snap.next) if snap.next else []
        return values

    def _pending_sync(self) -> list[dict]:
        """从审计表反查进入审批但未决策（仍在暂停）的线程。"""
        from services.audit_service import query as audit_query
        rows = audit_query(event_type=EventType.APPROVAL_PAUSED, size=200)["items"]
        out: list[dict] = []
        seen: set[str] = set()
        for r in rows:
            tid = r["thread_id"]
            if not tid or tid in seen:
                continue
            seen.add(tid)
            st = self._status_sync(tid)
            if st and st.get("paused"):
                out.append(st)
        return out

    # ----------------------- 异步包装 -----------------------

    async def submit(self, request: dict, actor: str, actor_role: str) -> dict:
        thread_id = request.get("thread_id") or f"WF-{uuid.uuid4().hex[:12]}"
        # P1-2：优先继承当前 HTTP 链路的 trace_id（中间件生成并绑定），缺失时才新建。
        # 这样「HTTP 响应头 X-Trace-Id → 节点日志 → 审计表」三者是同一个 id，
        # 真正做到一次请求一条链路可串联查询，而不是各层自造 id。
        trace_id = get_trace_id() or new_trace_id()
        state = _initial_state(request, thread_id, trace_id)
        return await asyncio.to_thread(self._submit_sync, state, actor, actor_role)

    async def decide(self, thread_id: str, approved: bool, comment: str,
                     actor: str, actor_role: str) -> dict:
        return await asyncio.to_thread(
            self._decide_sync, thread_id, approved, comment, actor, actor_role)

    async def status(self, thread_id: str) -> dict | None:
        return await asyncio.to_thread(self._status_sync, thread_id)

    async def pending(self) -> list[dict]:
        return await asyncio.to_thread(self._pending_sync)


# ----------------------------- 工具 -----------------------------

def _final_result(values: dict, paused: bool) -> str:
    """由工作流最终状态推导终态标签（供 Prometheus 统计成功率）。"""
    if paused:
        return "pending_approval"
    if values.get("approval_status") == "rejected":
        return "rejected"
    if values.get("execution_result") == "success":
        return "success"
    if values.get("execution_result") in ("failed", "error"):
        return "failed"
    return "unknown"


def _initial_state(request: dict, thread_id: str, trace_id: str) -> dict:
    return {
        "request_id": thread_id,
        "thread_id": thread_id,
        "trace_id": trace_id,
        "idempotency_key": request.get("idempotency_key") or thread_id,
        "request_type": request.get("request_type", "procurement"),
        "request_detail": request.get("request_detail", ""),
        "amount": float(request.get("amount", 0)),
        "department": request.get("department", ""),
        "current_phase": "analyze",
        "risk_level": "",
        "approval_status": "pending",
        "retry_count": 0,
        "error": None,
        "logs": [],
        "knowledge_context": "",
        "analysis_report": "",
        "execution_result": "",
        "final_report": "",
        "required_approvers": [],
    }


def _public_state(values: dict) -> dict:
    keys = ["request_id", "thread_id", "trace_id", "idempotency_key", "request_type",
            "request_detail", "amount",
            "department", "current_phase", "risk_level", "approval_status",
            "retry_count", "error", "knowledge_context", "analysis_report",
            "execution_result", "final_report", "required_approvers", "logs"]
    return {k: values.get(k) for k in keys}


# 单例
workflow_service = WorkflowService()
