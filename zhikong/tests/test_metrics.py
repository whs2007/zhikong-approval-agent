"""P1-3 Prometheus 真实指标测试：格式、内容、鉴权、路径基数控制。"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from core.metrics import (
    PENDING_APPROVALS,
    TOOL_CALLS,
    WORKFLOW_FINAL,
    WORKFLOW_SUBMISSIONS,
    normalize_path,
    record_tool_call,
    record_workflow_final,
    record_workflow_submission,
    render_metrics,
    set_breaker_open,
    set_pending_approvals,
)


def _metrics_text(client, token):
    r = client.get("/api/v1/metrics", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    return r.text


# ----------------------------- 端点格式 -----------------------------

def test_metrics_endpoint_is_prometheus_text(client, admin_token):
    """反例：/metrics 必须是 Prometheus 文本格式（不再是 JSON 全表 COUNT）。"""
    r = client.get("/api/v1/metrics", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    # Prometheus 文本格式特征：# HELP / # TYPE 行
    assert "# TYPE zhikong_http_requests_total counter" in r.text


def test_metrics_requires_auth(client):
    """指标含业务量等运营敏感信息，未鉴权访问必须拒绝。"""
    assert client.get("/api/v1/metrics").status_code in (401, 403)


def test_metrics_exposes_key_signals(client, admin_token):
    """覆盖四类关键信号：HTTP 流量/延迟、业务终态、工具韧性、审批积压。"""
    text = _metrics_text(client, admin_token)
    for name in (
        "zhikong_http_requests_total",
        "zhikong_http_request_duration_seconds",
        "zhikong_workflow_final_total",
        "zhikong_tool_calls_total",
        "zhikong_breaker_open",
        "zhikong_pending_approvals",
    ):
        assert name in text, name


def test_metrics_summary_json_kept(client, admin_token):
    """原 JSON 概览迁移到 /metrics/summary，避免破坏既有调用方。"""
    r = client.get("/api/v1/metrics/summary", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    data = r.json()["data"]
    assert "total_events" in data and "knowledge_chunks" in data


# ----------------------------- HTTP 计数真实增长 -----------------------------

def test_http_requests_counter_increases(client, admin_token):
    """发请求后计数必须真实增长（而非静态假指标）。"""
    def count_requests(text: str) -> float:
        total = 0.0
        for line in text.splitlines():
            if line.startswith("zhikong_http_requests_total{"):
                total += float(line.rsplit(" ", 1)[-1])
        return total

    before = count_requests(_metrics_text(client, admin_token))
    client.get("/api/v1/health")
    after = count_requests(_metrics_text(client, admin_token))
    assert after > before


# ----------------------------- 埋点函数 -----------------------------

def test_record_helpers_update_metrics():
    """埋点函数应写入 registry 并在 /metrics 文本中体现。"""
    record_workflow_submission("execute")
    record_workflow_final("success")
    record_tool_call("unit_tool", "success", 0.01)
    set_breaker_open(True)
    set_pending_approvals(7)

    body, content_type = render_metrics()
    text = body.decode()
    assert content_type.startswith("text/plain")
    assert 'zhikong_workflow_final_total{result="success"}' in text
    assert 'zhikong_tool_calls_total{outcome="success",tool="unit_tool"}' in text
    assert "zhikong_breaker_open 1.0" in text
    assert "zhikong_pending_approvals 7.0" in text

    # 复原以免影响其他断言
    set_breaker_open(False)
    set_pending_approvals(0)


# ----------------------------- 路径基数控制 -----------------------------

def test_path_normalization_prevents_label_explosion():
    """反例：含 thread_id 的原始路径必须归一化，否则 Prometheus series 失控。"""
    assert normalize_path("/api/v1/approval/decide/WF-4f96d403fbc2") == \
        "/api/v1/approval/decide/{WF}"
    # 同一路由不同 id 必须归并到同一标签
    a = normalize_path("/api/v1/approval/decide/WF-aaa111bbb222")
    b = normalize_path("/api/v1/approval/decide/WF-ccc333ddd444")
    assert a == b
    # 普通路径不应被误改
    assert normalize_path("/api/v1/health") == "/api/v1/health"
