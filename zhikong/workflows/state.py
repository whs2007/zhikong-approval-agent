"""工作流共享状态定义（TypedDict + Reducer）。

核心设计（对应教材难点 1）：
- 所有 Agent 共享同一份 WorkflowState，每个节点仅返回需更新的字段，LangGraph 自动合并。
- logs 使用 Annotated[list, operator.add] 累加而非覆盖，保证完整操作记录，可追溯。
"""
from __future__ import annotations

import operator
from typing import Annotated, Optional, TypedDict


class WorkflowState(TypedDict, total=False):
    # ---- 流程控制字段（驱动路由） ----
    logs: Annotated[list[str], operator.add]      # 自动累加
    current_phase: str        # analyze / approve / execute / done
    risk_level: str           # low / medium / high
    approval_status: str      # pending / approved / rejected
    retry_count: int
    error: Optional[str]
    trace_id: str
    # 幂等键：同一请求重复提交/重试时，保证副作用只发生一次
    idempotency_key: str

    # ---- 业务数据字段 ----
    request_id: str
    thread_id: str
    request_type: str         # procurement / expense / leave
    request_detail: str
    amount: float
    department: str
    knowledge_context: str
    analysis_report: str
    execution_result: str     # success / failed / error / cancelled
    final_report: str
    required_approvers: list[str]
