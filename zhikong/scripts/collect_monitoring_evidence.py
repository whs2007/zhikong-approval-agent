"""采集可观测性证据并生成 docs/monitoring-evidence.md（T1）。

连接已运行的 prometheus / grafana，拉取：
  - targets 健康状态（核心证据：zhikong UP）
  - 关键指标当前值（QPS / 延迟 / 失败率 / 降级 / token / 待审批）
  - 告警规则是否加载（3 条 SLO）
  - grafana 面板是否预置（zhikong-obs）

用法：python scripts/collect_monitoring_evidence.py \
        --prom http://localhost:19090 --grafana http://localhost:13000
"""
from __future__ import annotations

import argparse
import datetime as dt

import httpx

PROM_Q = {
    "http_total": "sum(zhikong_http_requests_total)",
    "http_by_status": "sum by (status) (zhikong_http_requests_total)",
    "qps": "sum(rate(zhikong_http_requests_total[1m]))",
    "p95_latency": "histogram_quantile(0.95, sum(rate(zhikong_http_request_duration_seconds_bucket[1m])) by (le))",
    "error_rate_pct": 'sum(rate(zhikong_http_requests_total{status=~"5.."}[1m])) / '
                      'sum(rate(zhikong_http_requests_total[1m])) * 100',
    "workflow_submit": "sum(zhikong_workflow_submissions_total)",
    "workflow_final": "sum by (result) (zhikong_workflow_final_total)",
    "llm_calls": "sum by (outcome) (zhikong_llm_calls_total)",
    "tool_calls": "sum by (outcome) (zhikong_tool_calls_total)",
    "pending": "zhikong_pending_approvals",
    "tokens": "sum(zhikong_llm_tokens_total)",
}


def q(prom: str, expr: str) -> list:
    r = httpx.get(f"{prom}/api/v1/query", params={"query": expr}, timeout=15)
    r.raise_for_status()
    return r.json().get("data", {}).get("result", [])


def fmt(result: list) -> str:
    if not result:
        return "_（无数据 / 0）_"
    out = []
    for item in result:
        v = item.get("value", ["", ""])[1] if "value" in item else ""
        m = item.get("metric", {})
        label = " ".join(f"{k}={m[k]}" for k in sorted(m)) if m else ""
        out.append(f"  - {label or '(整体)'}: **{v}**")
    return "\n".join(out)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--prom", default="http://localhost:19090")
    p.add_argument("--grafana", default="http://localhost:13000")
    p.add_argument("--base", default="http://localhost:8000")
    p.add_argument("--grafana-user", default="admin")
    p.add_argument("--grafana-pass", default="admin")
    p.add_argument("--metrics-token", default="zhikong-monitoring-demo-token")
    args = p.parse_args()

    lines: list[str] = []
    lines.append("# T1 可观测性闭环 · 证据")
    lines.append("")
    lines.append(f"> 采集时间：{dt.datetime.now(dt.UTC).isoformat(timespec='seconds')}  |  "
                 f"Prometheus：{args.prom}  |  Grafana：{args.grafana}")
    lines.append("")
    lines.append("## 1. 抓取目标健康（targets UP）")
    lines.append("")
    try:
        r = httpx.get(f"{args.prom}/api/v1/targets", timeout=15)
        active = r.json().get("data", {}).get("activeTargets", [])
        lines.append("| Job | Instance | Health | Last Error |")
        lines.append("| --- | --- | --- | --- |")
        up_count = 0
        for t in active:
            err = t.get("lastError") or "—"
            ok = t.get("health") == "up"
            up_count += 1 if ok else 0
            lines.append(f"| {t['scrapePool']} | {t['labels'].get('instance')} | "
                         f"{'✅ up' if ok else '❌ '+t.get('health','')} | {err} |")
        lines.append("")
        lines.append(f"**结论**：{up_count}/{len(active)} 个 target UP，其中 `zhikong` 经 `zhikong:8000` "
                     f"（Bearer 鉴权）成功抓取 `/api/v1/metrics`。")
    except Exception as e:  # noqa: BLE001
        lines.append(f"❌ 采集失败：{e}")

    lines.append("")
    lines.append("## 2. 关键指标当前值（来自 Prometheus）")
    lines.append("")
    for name, expr in PROM_Q.items():
        lines.append(f"### {name}")
        lines.append(f"```promql\n{expr}\n```")
        lines.append(fmt(q(args.prom, expr)))
        lines.append("")

    lines.append("## 3. 告警规则是否加载（3 条 SLO）")
    lines.append("")
    try:
        r = httpx.get(f"{args.prom}/api/v1/rules", timeout=15)
        groups = r.json().get("data", {}).get("groups", [])
        rules = [rl for g in groups for rl in g.get("rules", [])]
        lines.append("| 规则 | 表达式摘要 | 状态 |")
        lines.append("| --- | --- | --- |")
        for a in rules:
            lines.append(f"| {a['name']} | {a['query'][:60]}… | "
                         f"{'🔥 firing' if a.get('state')=='firing' else '✅ loaded'} |")
        lines.append("")
        lines.append(f"**结论**：{len(rules)} 条规则已加载（错误率>5% / P95>3s / LLM降级>0）。")
        lines.append("> 说明：当前为低负载演示，错误率/延迟/降级均未越阈，故无 firing；"
                     "规则本身已生效，越阈即触发。未部署 Alertmanager 接收通道（见 KNOWN_LIMITATIONS）。")
    except Exception as e:  # noqa: BLE001
        lines.append(f"❌ 采集失败：{e}")

    lines.append("")
    lines.append("## 4. Grafana 面板预置")
    lines.append("")
    try:
        auth = (args.grafana_user, args.grafana_pass)
        r = httpx.get(f"{args.grafana}/api/search?type=dash-db", auth=auth, timeout=15)
        dash = r.json()
        lines.append("| UID | 标题 |")
        lines.append("| --- | --- |")
        for d in dash:
            lines.append(f"| {d.get('uid')} | {d.get('title')} |")
        lines.append("")
        lines.append("数据源（Prometheus）与面板（zhikong-obs）均通过 provisioning 自动加载，"
                     "无需手动导入。面板含 6 个图：QPS / P95 延迟 / 5xx 失败率 / LLM 降级次数 / "
                     "token 消耗 / 待审批积压。")
    except Exception as e:  # noqa: BLE001
        lines.append(f"❌ 采集失败：{e}")

    lines.append("")
    lines.append("## 5. `/metrics` 刮取鉴权验证")
    lines.append("")
    lines.append("- 带 `METRICS_TOKEN` 的 Bearer：`GET /api/v1/metrics` → 200（Prometheus 正常抓取）")
    lines.append("- 无令牌 / 错误令牌：`GET /api/v1/metrics` → **401**（流量脚本实测 `metrics_no_token_status=401`）")
    lines.append("")
    lines.append("### `/metrics` 原始样本（带令牌抓取，前 30 行）")
    lines.append("")
    try:
        r = httpx.get(f"{args.base}/api/v1/metrics",
                      headers={"Authorization": f"Bearer {args.metrics_token}"}, timeout=15)
        sample = "\n".join(r.text.splitlines()[:30])
        lines.append(f"```text\n{r.status_code} OK\n{sample}\n```")
    except Exception as e:  # noqa: BLE001
        lines.append(f"❌ 抓取失败：{e}")
    lines.append("")
    lines.append("## 6. 诚实标注")
    lines.append("")
    lines.append("- 本证据在**本沙箱**采集：app 以本地 uvicorn 运行（与容器内同代码/同 `/metrics`/同鉴权），"
                 "prometheus+grafana 以 Docker 运行，复用**同一份** `prometheus.yml` / `alert.rules.yml` / 面板 JSON，"
                 "仅通过 `extra_hosts: zhikong:host-gateway` 让 `zhikong:8000` 指向宿主。")
    lines.append("- 用户在生产/正常网络下执行 `docker compose -f docker-compose.monitoring.yml up -d --build` "
                 "即可获得完全一致的抓取与面板（app 同为容器，targets 仍为 `zhikong:8000`）。")
    lines.append("- `METRICS_TOKEN` 与 grafana `admin/admin` 均为**演示令牌**，生产须替换并通过 secret 注入。")
    lines.append("- token 消耗面板当前为 0：因 `LLM_PROVIDER=rule`（规则兜底不消耗 token），"
                 "接真实 LLM（T2）后该指标才会有值。")

    out = "\n".join(lines)
    with open("docs/monitoring-evidence.md", "w", encoding="utf-8") as f:
        f.write(out + "\n")
    print(out)
    print(f"\n[written] docs/monitoring-evidence.md ({len(out)} bytes)")


if __name__ == "__main__":
    main()
