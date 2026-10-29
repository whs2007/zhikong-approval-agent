# 「智控」企业级智能工作流平台 — API 接口设计

> 文档版本：v1.0
> 前置文档：《02-系统架构设计》《03-数据库与持久化设计》
> 基址：`http://<host>:<port>/api/v1`

---

## 1. 通用约定

### 1.1 鉴权
- 除 `POST /auth/login`、`GET /health`、`GET /`(前端) 外，所有接口需在 `Authorization: Bearer <JWT>` 头中携带令牌。
- 角色要求见各接口"权限"列。

### 1.2 统一响应
```json
{ "code": 0, "message": "ok", "data": { }, "trace_id": "..." }
```
- `code=0` 成功；非 0 见错误码表。
- 列表类 `data` 含 `{ "items": [...], "total": N }`。

### 1.3 错误码

| code | 含义 |
|------|------|
| 0 | 成功 |
| 40001 | 参数校验失败 |
| 40101 | 未认证/令牌无效 |
| 40301 | 无权限（角色不符） |
| 40401 | 工作流/资源不存在 |
| 40901 | 状态冲突（如重复审批） |
| 50001 | 服务器内部错误 |

### 1.4 数据模型（摘要）
- `request_type`: `procurement` / `expense` / `leave`
- `risk_level`: `low` / `medium` / `high`
- `approval_status`: `pending` / `approved` / `rejected`
- `current_phase`: `analyze` / `approve` / `execute` / `done`

---

## 2. 接口清单

### 2.1 认证 `auth`

#### POST `/auth/login`
登录获取 JWT。
- 权限：匿名
- 请求：
```json
{ "username": "admin", "password": "admin123" }
```
- 响应：
```json
{ "code":0, "data": { "access_token":"<jwt>", "token_type":"bearer", "role":"admin", "username":"admin" } }
```

#### GET `/auth/me`
返回当前用户信息。
- 权限：`any`

---

### 2.2 工作流 `workflow`

#### POST `/workflow/submit`
提交申请，执行至首个中断点（高风险则停在审批前）。
- 权限：`applicant` / `admin`
- 请求：
```json
{
  "request_type": "procurement",
  "request_detail": "采购50台笔记本电脑用于研发",
  "amount": 300000.0,
  "department": "技术部"
}
```
- 响应：`data` 含 `thread_id`、`risk_level`、`current_phase`、`approval_status`、`analysis_report`、`knowledge_context`、`logs`。

#### GET `/workflow/status/{thread_id}`
查询某工作流当前状态快照。
- 权限：`applicant`(本人)/`approver`/`admin`

#### GET `/workflow/pending`
待审批列表（current_phase=approve 且未决策）。
- 权限：`approver` / `admin`
- 响应：`data.items` 为数组，每项含 `thread_id`、摘要、风险、金额、提交时间。

#### GET `/workflow/list`
工作流列表（分页、可按状态/类型过滤）。
- 权限：`admin`

---

### 2.3 审批 `approval`

#### POST `/approval/decide/{thread_id}`
审批人决策（批准/驳回），恢复工作流执行。
- 权限：`approver` / `admin`
- 请求：
```json
{ "approved": true, "comment": "同意采购，预算内" }
```
- 响应：`data` 含恢复后的最终状态与 `final_report`。

#### POST `/approval/timeout-scan`（进阶）
扫描超时待审批并触发提醒/上报。
- 权限：`admin`

---

### 2.4 审计 `audit`

#### GET `/audit/logs`
查询审计日志（多条件过滤）。
- 权限：`admin`
- 参数：`thread_id?`、`actor?`、`event_type?`、`start?`、`end?`、`page?`、`size?`
- 响应：`data.items` 为审计事件数组。

#### GET `/audit/export`
导出审计日志（CSV/JSON）。
- 权限：`admin`
- 参数同上 + `format=csv|json`

#### GET `/audit/trace/{trace_id}`
按追踪号查完整事件链。
- 权限：`admin`

---

### 2.5 系统 `system`

#### GET `/health`
健康检查：返回 `{"status":"ok","checks":{"db":true,"checkpointer":true}}`。

#### GET `/metrics`
基础指标：`total`/`success`/`pending`/`failed` 计数、平均耗时。

#### POST `/knowledge/reload`
重载知识库。
- 权限：`admin`

---

## 3. 典型调用时序（高风险审批）

```
申请人 ──POST /auth/login──▶ 拿到 JWT
申请人 ──POST /workflow/submit──▶ 返回 thread_id, current_phase=approve (已暂停)
审批人 ──GET  /workflow/pending──▶ 看到待办
审批人 ──POST /approval/decide/{id}──▶ 恢复执行, 返回 final_report
任何人 ──GET  /audit/logs?thread_id=...──▶ 完整审计链
```

> 每一步均经 `AuditService` 写入审计事件，`trace_id` 贯穿。

---

## 4. OpenAPI
FastAPI 自动生成 `/docs`(Swagger) 与 `/openapi.json`，无需手写。接口模型由 `schemas/` 的 Pydantic 类驱动。
