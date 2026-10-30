"""生成智控 Grafana 面板（T1 可观测性闭环）。

产出 monitoring/grafana/dashboards/zhikong.json，覆盖六个核心可观测信号：
  QPS / P95 延迟 / 5xx 失败率 / LLM 降级次数 / token 消耗 / 待审批积压。

纯标准 Grafana dashboard schema（schemaVersion 39, Grafana 10/11 兼容），
不依赖任何第三方库，保证产出的是合法 JSON。
"""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "grafana" / "provisioning" / "dashboards" / "zhikong.json"

PROM = "Prometheus"


def timeseries(title: str, expr: str, grid, legend="", unit="", color="#3274D9") -> dict:
    return {
        "id": abs(hash(expr)) % 100000,
        "type": "timeseries",
        "title": title,
        "gridPos": {"h": 8, "w": 12, "x": grid[0], "y": grid[1]},
        "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "custom": {
                    "drawStyle": "line",
                    "lineWidth": 2,
                    "fillOpacity": 10,
                    "lineColor": color,
                },
            },
            "overrides": [],
        },
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}},
        "targets": [
            {
                "refId": "A",
                "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
                "expr": expr,
                "legendFormat": legend or title,
            }
        ],
    }


def stat(title: str, expr: str, grid, unit="", color="#7EB26D") -> dict:
    return {
        "id": abs(hash(expr)) % 100000,
        "type": "stat",
        "title": title,
        "gridPos": {"h": 8, "w": 12, "x": grid[0], "y": grid[1]},
        "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "color": {"mode": "fixed", "fixedColor": color},
                "custom": {"displayMode": "bigvalue"},
            },
            "overrides": [],
        },
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"]},
            "orientation": "auto",
        },
        "targets": [
            {
                "refId": "A",
                "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"},
                "expr": expr,
                "legendFormat": title,
            }
        ],
    }


def main() -> None:
    panels = [
        timeseries(
            "QPS（每秒请求数）",
            "sum(rate(zhikong_http_requests_total[1m]))",
            (0, 0), legend="{{method}} {{path}}", unit="reqps", color="#3274D9",
        ),
        timeseries(
            "HTTP P95 延迟",
            "histogram_quantile(0.95, sum(rate(zhikong_http_request_duration_seconds_bucket[1m])) by (le))",
            (12, 0), legend="P95", unit="s", color="#EAB839",
        ),
        timeseries(
            "5xx 失败率（%）",
            "sum(rate(zhikong_http_requests_total{status=~\"5..\"}[1m])) / "
            "sum(rate(zhikong_http_requests_total[1m])) * 100",
            (0, 8), legend="error_rate", unit="percent", color="#E24D42",
        ),
        stat(
            "LLM 降级次数（近1h）",
            "sum(increase(zhikong_llm_calls_total{outcome=\"fallback\"}[1h]))",
            (12, 8), unit="short", color="#E24D42",
        ),
        timeseries(
            "LLM token 消耗速率（/min）",
            "sum(rate(zhikong_llm_tokens_total[1m])) by (type)",
            (0, 16), legend="{{type}}", unit="short", color="#6ED0E0",
        ),
        stat(
            "待审批积压数",
            "zhikong_pending_approvals",
            (12, 16), unit="short", color="#7EB26D",
        ),
    ]

    dashboard = {
        "title": "智控 · 可观测性面板",
        "uid": "zhikong-obs",
        "schemaVersion": 39,
        "version": 1,
        "editable": True,
        "time": {"from": "now-1h", "to": "now"},
        "timepicker": {},
        "templating": {
            "list": [
                {
                    "name": "DS_PROMETHEUS",
                    "type": "datasource",
                    "query": "prometheus",
                    "current": {},
                    "label": "Prometheus",
                }
            ]
        },
        "annotations": {"list": [{"builtIn": 1, "type": "grafana"}]},
        "panels": panels,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(dashboard, ensure_ascii=False, indent=2), encoding="utf-8")
    # 校验：能被 json 解析
    json.loads(OUT.read_text(encoding="utf-8"))
    print(f"dashboard written: {OUT} ({OUT.stat().st_size} bytes, {len(panels)} panels)")


if __name__ == "__main__":
    main()
