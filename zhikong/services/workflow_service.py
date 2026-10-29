"""工作流服务层：封装 LangGraph app 的提交/暂停/恢复/查询。

- 同步 invoke（SqliteSaver 为同步 Checkpointer），在异步 API 中经 asyncio.to_thread 调用。
- submit：首次 invoke；若低风险提示自动放行则一次跑完；若进入审批闸门则暂停返回待审批。
- decide：审批人决策 -> update_state -> resume，并记录真实操作者审计。
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any, Optional

from langgraph.types import Command

from core.config import settings
from core.logging import logger
from services.audit_service import EventType, append, new_trace_id
from workflows.graph import build_workflow


class WorkflowService:
    def __init__(self) -> None:
        self.app = build_workflow()

    # ----------------------- 同步核心（供 to_thread 调用） -----------------------

    def _submit_sync(self, state: dict, actor: str, actor_role: str) -> dict:
        thread_id = state["thread_id"]
        config = {"configurable": {"thread_id": thread_id}}
        append(EventType.WORKFLOW_SUBMIT, actor=actor, actor_role=actor_role,
               thread_id=thread_id, trace_id=state.get("trace_id"),
               detail={"request_type": state["request_type"], "amount": state["amount"],
                       "department": state.get("department")})
        result = self.app.invoke(state, config)
        snap = self.app.get_state(config)
        paused = bool(snap.next)
        if paused:
            # 进入人工审批闸门，登记暂停审计
            append(EventType.APPROVAL_PAUSED, actor="system", actor_role="system",
                   thread_id=thread_id, trace_id=state.get("trace_id"),
                   node="approval_gate",
                   detail={"risk_level": result.get("risk_level"),
                           "amount": state.get("amount")})
        return {"state": _public_state(result), "paused": paused,
                "next": list(snap.next) if snap.next else []}

    def _decide_sync(self, thread_id: str, approved: bool, comment: str,
                     actor: str, actor_role: str) -> dict:
        config = {"configurable": {"thread_id": thread_id}}
        # 用 Command(resume=...) 将审批结论传入 interrupt() 恢复执行
        result = self.app.invoke(
            Command(resume={"approved": approved, "comment": comment,
                            "actor": actor, "actor_role": actor_role}),
            config,
        )
        append(EventType.APPROVAL_DECISION, actor=actor, actor_role=actor_role,
               thread_id=thread_id,
               detail={"decision": "approved" if approved else "rejected",
                       "comment": comment})
        return _public_state(result)

    def _status_sync(self, thread_id: str) -> Optional[dict]:
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
        trace_id = new_trace_id()
        state = _initial_state(request, thread_id, trace_id)
        return await asyncio.to_thread(self._submit_sync, state, actor, actor_role)

    async def decide(self, thread_id: str, approved: bool, comment: str,
                     actor: str, actor_role: str) -> dict:
        return await asyncio.to_thread(
            self._decide_sync, thread_id, approved, comment, actor, actor_role)

    async def status(self, thread_id: str) -> Optional[dict]:
        return await asyncio.to_thread(self._status_sync, thread_id)

    async def pending(self) -> list[dict]:
        return await asyncio.to_thread(self._pending_sync)


# ----------------------------- 工具 -----------------------------

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
