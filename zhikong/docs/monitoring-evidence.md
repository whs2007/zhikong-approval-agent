# T1 可观测性闭环 · 证据

> 采集时间：2026-10-30T03:58:41+00:00  |  Prometheus：http://localhost:19090  |  Grafana：http://localhost:13000

## 1. 抓取目标健康（targets UP）

| Job | Instance | Health | Last Error |
| --- | --- | --- | --- |
| prometheus | localhost:9090 | ✅ up | — |
| zhikong | zhikong:8000 | ✅ up | — |

**结论**：2/2 个 target UP，其中 `zhikong` 经 `zhikong:8000` （Bearer 鉴权）成功抓取 `/api/v1/metrics`。

## 2. 关键指标当前值（来自 Prometheus）

### http_total
```promql
sum(zhikong_http_requests_total)
```
  - (整体): **339**

### http_by_status
```promql
sum by (status) (zhikong_http_requests_total)
```
  - status=200: **258**
  - status=401: **81**

### qps
```promql
sum(rate(zhikong_http_requests_total[1m]))
```
  - (整体): **0.06666666666666665**

### p95_latency
```promql
histogram_quantile(0.95, sum(rate(zhikong_http_request_duration_seconds_bucket[1m])) by (le))
```
  - (整体): **0.004749999999999999**

### error_rate_pct
```promql
sum(rate(zhikong_http_requests_total{status=~"5.."}[1m])) / sum(rate(zhikong_http_requests_total[1m])) * 100
```
_（无数据 / 0）_

### workflow_submit
```promql
sum(zhikong_workflow_submissions_total)
```
  - (整体): **20**

### workflow_final
```promql
sum by (result) (zhikong_workflow_final_total)
```
  - result=pending_approval: **16**
  - result=success: **4**

### llm_calls
```promql
sum by (outcome) (zhikong_llm_calls_total)
```
_（无数据 / 0）_

### tool_calls
```promql
sum by (outcome) (zhikong_tool_calls_total)
```
  - outcome=success: **20**
  - outcome=failed: **14**

### pending
```promql
zhikong_pending_approvals
```
  - __name__=zhikong_pending_approvals instance=zhikong:8000 job=zhikong tier=app: **0**

### tokens
```promql
sum(zhikong_llm_tokens_total)
```
_（无数据 / 0）_

## 3. 告警规则是否加载（3 条 SLO）

| 规则 | 表达式摘要 | 状态 |
| --- | --- | --- |
| ErrorRateHigh | (sum(rate(zhikong_http_requests_total{status=~"5.."}[5m])) /… | ✅ loaded |
| LatencyP95High | histogram_quantile(0.95, sum by (le) (rate(zhikong_http_requ… | ✅ loaded |
| LLMDegradation | increase(zhikong_llm_calls_total{outcome="fallback"}[10m]) >… | ✅ loaded |

**结论**：3 条规则已加载（错误率>5% / P95>3s / LLM降级>0）。
> 说明：当前为低负载演示，错误率/延迟/降级均未越阈，故无 firing；规则本身已生效，越阈即触发。未部署 Alertmanager 接收通道（见 KNOWN_LIMITATIONS）。

## 4. Grafana 面板预置

| UID | 标题 |
| --- | --- |
| zhikong-obs | 智控 · 可观测性面板 |

数据源（Prometheus）与面板（zhikong-obs）均通过 provisioning 自动加载，无需手动导入。面板含 6 个图：QPS / P95 延迟 / 5xx 失败率 / LLM 降级次数 / token 消耗 / 待审批积压。

## 5. `/metrics` 刮取鉴权验证

- 带 `METRICS_TOKEN` 的 Bearer：`GET /api/v1/metrics` → 200（Prometheus 正常抓取）
- 无令牌 / 错误令牌：`GET /api/v1/metrics` → **401**（流量脚本实测 `metrics_no_token_status=401`）

### `/metrics` 原始样本（带令牌抓取，前 30 行）

```text
200 OK
# HELP zhikong_http_requests_total HTTP 请求总数
# TYPE zhikong_http_requests_total counter
zhikong_http_requests_total{method="GET",path="/api/v1/metrics",status="200"} 99.0
zhikong_http_requests_total{method="POST",path="/api/v1/auth/login",status="200"} 4.0
zhikong_http_requests_total{method="POST",path="/api/v1/workflow/submit",status="200"} 20.0
zhikong_http_requests_total{method="POST",path="/api/v1/approval/decide/{WF}",status="200"} 16.0
zhikong_http_requests_total{method="GET",path="/api/v1/health",status="200"} 120.0
zhikong_http_requests_total{method="GET",path="/api/v1/workflow/list",status="401"} 80.0
zhikong_http_requests_total{method="GET",path="/api/v1/metrics",status="401"} 1.0
# HELP zhikong_http_requests_created HTTP 请求总数
# TYPE zhikong_http_requests_created gauge
zhikong_http_requests_created{method="GET",path="/api/v1/metrics",status="200"} 1.793332143204544e+09
zhikong_http_requests_created{method="POST",path="/api/v1/auth/login",status="200"} 1.7933321464441714e+09
zhikong_http_requests_created{method="POST",path="/api/v1/workflow/submit",status="200"} 1.7933321514211664e+09
zhikong_http_requests_created{method="POST",path="/api/v1/approval/decide/{WF}",status="200"} 1.7933321537972407e+09
zhikong_http_requests_created{method="GET",path="/api/v1/health",status="200"} 1.7933321590614734e+09
zhikong_http_requests_created{method="GET",path="/api/v1/workflow/list",status="401"} 1.7933321593866482e+09
zhikong_http_requests_created{method="GET",path="/api/v1/metrics",status="401"} 1.7933321594295897e+09
# HELP zhikong_http_request_duration_seconds HTTP 请求耗时（秒）
# TYPE zhikong_http_request_duration_seconds histogram
zhikong_http_request_duration_seconds_bucket{le="0.005",method="GET",path="/api/v1/metrics"} 99.0
zhikong_http_request_duration_seconds_bucket{le="0.01",method="GET",path="/api/v1/metrics"} 99.0
zhikong_http_request_duration_seconds_bucket{le="0.025",method="GET",path="/api/v1/metrics"} 99.0
zhikong_http_request_duration_seconds_bucket{le="0.05",method="GET",path="/api/v1/metrics"} 99.0
zhikong_http_request_duration_seconds_bucket{le="0.075",method="GET",path="/api/v1/metrics"} 100.0
zhikong_http_request_duration_seconds_bucket{le="0.1",method="GET",path="/api/v1/metrics"} 100.0
zhikong_http_request_duration_seconds_bucket{le="0.25",method="GET",path="/api/v1/metrics"} 100.0
zhikong_http_request_duration_seconds_bucket{le="0.5",method="GET",path="/api/v1/metrics"} 100.0
zhikong_http_request_duration_seconds_bucket{le="0.75",method="GET",path="/api/v1/metrics"} 100.0
zhikong_http_request_duration_seconds_bucket{le="1.0",method="GET",path="/api/v1/metrics"} 100.0
```

## 6. 诚实标注

- 本证据在**本沙箱**采集：app 以本地 uvicorn 运行（与容器内同代码/同 `/metrics`/同鉴权），prometheus+grafana 以 Docker 运行，复用**同一份** `prometheus.yml` / `alert.rules.yml` / 面板 JSON，仅通过 `extra_hosts: zhikong:host-gateway` 让 `zhikong:8000` 指向宿主。
- 用户在生产/正常网络下执行 `docker compose -f docker-compose.monitoring.yml up -d --build` 即可获得完全一致的抓取与面板（app 同为容器，targets 仍为 `zhikong:8000`）。
- `METRICS_TOKEN` 与 grafana `admin/admin` 均为**演示令牌**，生产须替换并通过 secret 注入。
- token 消耗面板当前为 0：因 `LLM_PROVIDER=rule`（规则兜底不消耗 token），接真实 LLM（T2）后该指标才会有值。
