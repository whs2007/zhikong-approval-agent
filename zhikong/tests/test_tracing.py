"""P1-2 全链路 trace_id 测试：中间件透传/生成、上下文绑定还原、日志自动携带链路 ID。"""
from __future__ import annotations

import json
import logging
import sys

sys.path.insert(0, ".")

from core.logging import (
    JsonFormatter,
    TraceContextFilter,
    bind_trace,
    get_thread_id,
    get_trace_id,
    new_trace_id,
    reset_trace,
)

# ----------------------------- 上下文绑定 -----------------------------

def test_bind_and_reset_restores_context():
    """反例：请求结束必须归还上下文，否则线程复用会导致链路串号。"""
    assert get_trace_id() == ""
    tid_token, th_token = bind_trace(trace_id="trace-A", thread_id="WF-1")
    assert get_trace_id() == "trace-A"
    assert get_thread_id() == "WF-1"

    reset_trace(tid_token, th_token)
    assert get_trace_id() == ""      # 已还原
    assert get_thread_id() == ""


def test_bind_partial_only_sets_provided_fields():
    """只绑定显式传入的项，未指定的维持原值。"""
    tid_token, _ = bind_trace(trace_id="trace-B")
    assert get_trace_id() == "trace-B"
    assert get_thread_id() == ""
    reset_trace(tid_token, None)


def test_new_trace_id_is_hex32():
    tid = new_trace_id()
    assert len(tid) == 32
    assert tid == tid.strip()


# ----------------------------- 日志自动携带 -----------------------------

def test_json_log_includes_trace_and_thread_ids():
    """核心：业务代码无需手动拼 id，日志自动携带 trace_id/thread_id。"""
    tid = new_trace_id()
    bind_trace(trace_id=tid, thread_id="WF-42")
    try:
        record = logging.LogRecord("zhikong", logging.INFO, __file__, 1, "节点日志", None, None)
        assert TraceContextFilter().filter(record) is True  # Filter 注入字段
        payload = json.loads(JsonFormatter().format(record))
    finally:
        # 注意 bind 返回 token 才能 reset；此处用空值覆盖保持测试隔离
        bind_trace(trace_id="", thread_id="")

    assert payload["trace_id"] == tid
    assert payload["thread_id"] == "WF-42"
    assert payload["msg"] == "节点日志"
    assert payload["level"] == "INFO"


def test_json_log_omits_empty_trace_fields():
    """无链路上下文时不输出空字段，避免噪声。"""
    bind_trace(trace_id="", thread_id="")
    record = logging.LogRecord("zhikong", logging.INFO, __file__, 1, "无链路日志", None, None)
    TraceContextFilter().filter(record)
    payload = json.loads(JsonFormatter().format(record))
    assert "trace_id" not in payload
    assert "thread_id" not in payload


# ----------------------------- HTTP 中间件 -----------------------------

def test_trace_id_generated_and_returned(client):
    """反例：未提供 X-Trace-Id 时应自动生成并回写响应头（32 位 hex）。"""
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    tid = r.headers.get("X-Trace-Id")
    assert tid is not None
    assert len(tid) == 32


def test_trace_id_propagated_from_request_header(client):
    """跨服务串联：入站 X-Trace-Id 必须被原样透传回写。"""
    r = client.get("/api/v1/health", headers={"X-Trace-Id": "downstream-trace-001"})
    assert r.status_code == 200
    assert r.headers.get("X-Trace-Id") == "downstream-trace-001"


def test_trace_id_not_leaked_between_requests(client):
    """反例：两个请求若互不提供 X-Trace-Id，应各自获得不同 trace_id（不串号）。"""
    a = client.get("/api/v1/health").headers.get("X-Trace-Id")
    b = client.get("/api/v1/health").headers.get("X-Trace-Id")
    assert a and b
    assert a != b


def test_trace_id_header_on_error_response(client):
    """即使 4xx 也要回写 X-Trace-Id，便于客户端用该 id 报障。"""
    r = client.get("/api/v1/does-not-exist")
    assert r.status_code == 404
    assert r.headers.get("X-Trace-Id")


def test_workflow_inherits_http_trace_id(client, applicant_token):
    """核心闭环：提交的响应头 X-Trace-Id 与工作流 trace_id 必须是同一个 id。

    否则「HTTP 日志 / 节点日志 / 审计表」各用各的 id，等于链路仍然断裂。
    """
    r = client.post("/api/v1/workflow/submit",
                    headers={"Authorization": f"Bearer {applicant_token}"},
                    json={"request_type": "procurement", "request_detail": "采购服务器",
                          "amount": 100.0})
    assert r.status_code == 200, r.text
    http_trace = r.headers.get("X-Trace-Id")
    wf_trace = r.json()["data"]["trace_id"]
    assert http_trace
    assert wf_trace == http_trace


def test_workflow_trace_id_follows_inbound_header(client, applicant_token):
    """入站 X-Trace-Id（下游/网关传入）应贯穿到工作流 trace_id。"""
    r = client.post("/api/v1/workflow/submit",
                    headers={"Authorization": f"Bearer {applicant_token}",
                             "X-Trace-Id": "gateway-trace-777"},
                    json={"request_type": "procurement", "request_detail": "采购",
                          "amount": 100.0})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["trace_id"] == "gateway-trace-777"
    assert r.headers.get("X-Trace-Id") == "gateway-trace-777"
