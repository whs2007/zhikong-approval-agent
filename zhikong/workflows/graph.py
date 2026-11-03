"""LangGraph 工作流编排（StateGraph 编译）。"""
from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from core.config import settings
from core.logging import logger
from core.sqlite_utils import connect
from workflows.agents import (
    analyst_node,
    approval_gate_node,
    executor_node,
    monitor_node,
    route_after_analysis,
    route_after_approval_gate,
    route_after_execution,
)
from workflows.state import WorkflowState

# 长驻资源持有表：PostgresSaver 的连接一旦被 GC 回收就会失效，
# 必须由模块级引用保活（详见 build_checkpointer 内的注释）。
_KEEPALIVE: dict[str, Any] = {}


def build_checkpointer():
    """构建持久化 Checkpointer。

    显式支持 sqlite | memory | postgres；未知值直接报错——不再静默回退 sqlite，
    避免"声明 postgres、实际写本地文件"的配置与实现不一致（P0-1）。
    """
    cp = (settings.CHECKPOINTER or "sqlite").strip().lower()

    if cp == "memory":
        from langgraph.checkpoint.memory import MemorySaver
        logger.warning("[checkpointer] 使用 MemorySaver（易失，仅测试）")
        return MemorySaver()

    if cp == "sqlite":
        from langgraph.checkpoint.sqlite import SqliteSaver
        # durable=False：checkpoint 是中间状态，可从审计事件重建，用 WAL+NORMAL 换吞吐
        conn = connect(settings.CHECKPOINTER_DB, durable=False)
        logger.info(f"[checkpointer] 使用 SqliteSaver -> {settings.CHECKPOINTER_DB}")
        return SqliteSaver(conn)

    if cp == "postgres":
        if not settings.POSTGRES_URL:
            raise RuntimeError(
                "CHECKPOINTER=postgres 但未配置 POSTGRES_URL；"
                "请设置 POSTGRES_URL，或将 CHECKPOINTER 设为 sqlite。")
        try:
            from langgraph.checkpoint.postgres import PostgresSaver
            from psycopg import Connection
            from psycopg.rows import dict_row
        except ImportError as e:
            raise RuntimeError(
                "CHECKPOINTER=postgres 需要安装 langgraph-checkpoint-postgres 与 psycopg。"
                "注意：本项目 auth/audit 业务库仍为 SQLite，水平扩展前需一并迁移，"
                "否则多副本状态仍不一致（见 docs/03-数据库与持久化设计.md）。"
            ) from e
        # 连接必须自己持有并保活，不能用 PostgresSaver.from_conn_string().__enter__()：
        # 那是一个 @contextmanager，.__enter__() 之后若不保留该生成器对象，
        # 它会被 GC 回收并触发 finally 关闭连接，紧接着 setup() 就报
        # "the connection is closed"（本仓库实测踩过，见 docs/20）。
        conn = Connection.connect(
            settings.POSTGRES_URL, autocommit=True, prepare_threshold=0,
            row_factory=dict_row)
        _KEEPALIVE["pg_conn"] = conn
        saver = PostgresSaver(conn)
        saver.setup()  # 幂等建表（checkpoints / checkpoint_blobs / checkpoint_writes）
        logger.info("[checkpointer] 使用 PostgresSaver（连接长驻至进程退出）")
        return saver

    raise ValueError(f"未知 CHECKPOINTER={cp}，仅支持 sqlite | memory | postgres")


def build_workflow():
    graph = StateGraph(WorkflowState)
    graph.add_node("analyst", analyst_node)
    graph.add_node("approval_gate", approval_gate_node)
    graph.add_node("executor", executor_node)
    graph.add_node("monitor", monitor_node)

    graph.add_edge(START, "analyst")
    graph.add_conditional_edges(
        "analyst", route_after_analysis,
        {"executor": "executor", "approval_gate": "approval_gate"},
    )
    graph.add_conditional_edges(
        "approval_gate", route_after_approval_gate,
        {"executor": "executor", "monitor": "monitor"},
    )
    graph.add_conditional_edges(
        "executor", route_after_execution,
        {"executor": "executor", "monitor": "monitor"},
    )
    graph.add_edge("monitor", END)

    return graph.compile(checkpointer=build_checkpointer())
