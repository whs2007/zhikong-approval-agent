# 「智控」企业级智能工作流平台

> 基于 **LangGraph 多 Agent 协作 + RAG 知识引擎 + Human-in-the-Loop 审批 + 图级别循环重试 + 全链路审计** 的企业级智能工作流平台。
>
> 本项目由简历中的教学版（教材《第 11 章 · 项目五》）按 **分析 → 技术/需求文档 → 企业级开发 → 过程审计** 的顺序升级而来。

---

## 一、为什么做这次升级

教材原版是"能跑通机制的最小示例"，存在生产级缺口。本项目将其升级为企业级：

| 维度 | 教材教学版 | 本企业版 |
|------|-----------|----------|
| 知识引擎 | 关键词匹配 | 真实向量 RAG（可离线 n-gram Embedding，可切换大模型） |
| 状态持久化 | `MemorySaver`（重启即丢） | `SqliteSaver` 文件持久化（支持跨天/重启恢复） |
| 审计 | 无 | 独立 append-only 审计子系统（全链路留痕、可导出） |
| 认证授权 | 无 | JWT + RBAC（申请人/审批人/管理员） |
| 架构 | 单文件拼装 | 分层架构（core / services / workflows / api / schemas / models） |
| 可观测 | `print` | 结构化 JSON 日志 + 健康检查 + 指标 |
| 部署 | `python main.py` | Docker / docker-compose |

---

## 二、目录结构

```
.
├── docs/                      # 技术文档（分析/设计/审计/计划/部署/过程审计）
│   ├── 01-需求分析说明书.md
│   ├── 02-系统架构设计.md
│   ├── 03-数据库与持久化设计.md
│   ├── 04-API接口设计.md
│   ├── 05-审计与安全设计.md
│   ├── 06-开发计划与里程碑.md
│   ├── 07-测试方案与验收.md
│   ├── 08-部署与运维.md
│   └── 09-过程审计报告.md
└── zhikong/                   # 企业级源码
    ├── core/                  # 配置 / 安全(JWT) / 日志
    ├── services/              # 审计 / 鉴权 / 知识引擎 / 工作流服务
    ├── workflows/             # 状态 / 可插拔 Provider / 节点 / 图编排
    ├── api/                   # 路由 / 依赖注入 / 应用装配
    ├── schemas/               # Pydantic 数据契约
    ├── models/                # 用户/审计模型（schema 文档）
    ├── frontend/index.html    # 审批看板（原生 SPA，无构建）
    ├── data/knowledge/        # 企业规章制度（RAG 语料）
    ├── tests/                 # pytest 用例
    ├── requirements.txt  .env.example  Dockerfile  docker-compose.yml
    └── README.md
```

---

## 三、快速开始

```bash
cd zhikong
pip install -r requirements.txt
# 默认离线配置即可运行（本地 Embedding + 规则兜底 + SQLite 持久化）
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

- 前端看板：`http://localhost:8000/`
- API 文档：`http://localhost:8000/docs`
- **账号说明**：代码库内不含任何可用口令。演示环境需显式开启种子并自行注入口令：
  ```bash
  DEMO_SEED=1 DEMO_ADMIN_PASSWORD='你的强口令' DEMO_APPROVER_PASSWORD='...' DEMO_APPLICANT_PASSWORD='...' \
    uvicorn api.main:app
  ```
  不提供 `DEMO_*_PASSWORD` 时会**随机生成**并打印在启动日志中。生产环境请用
  `POST /api/v1/auth/init-admin` 冷启动引导首个管理员（`DEMO_SEED` 在生产会被拒绝启动）。

> 未配置任何外部 API Key 也能完整跑通演示（RAG 用本地 n-gram 向量检索，分析用规则兜底）。配置 `LLM_PROVIDER=openai/dashscope`、`EMBEDDING_PROVIDER=st` 即可切换为真实大模型，业务代码零改动。

---

## 四、核心能力验证（已通过 12 项 pytest）

- ✅ 低风险申请自动执行（无人工干预）
- ✅ 高风险申请暂停 → 审批人批准 → 自动恢复执行
- ✅ 审批驳回 → 流程结束、不执行
- ✅ 图级别循环重试（每次失败有快照与日志，第 3 次强制成功）
- ✅ 跨重启/跨天恢复（同一 checkpoint 文件新建实例仍可恢复）
- ✅ 全链路审计留痕（提交/风险/暂停/决策/执行/完成 + 登录）
- ✅ 鉴权（无 token→401，越权→403，审计仅管理员可见）

---

## 五、关键技术决策（ADR 摘要）

1. **可插拔 Provider，默认离线**：LLM/Embedding/向量库均为接口化，默认零依赖离线运行。
2. **持久化替代内存**：`SqliteSaver` 替代 `MemorySaver`，满足跨天/重启恢复。
3. **条件中断替代全局中断**：用 `langgraph.types.interrupt` 仅在审批闸门暂停，避免重试循环被误暂停。
4. **独立审计子系统**：append-only 审计库，与业务/Checkpointer 物理分离，可导出。
5. **告警必须可达**：规则 + Alertmanager + Webhook 接收端构成闭环，做过真实故障演练
   （见 `docs/22-告警闭环验证.md`）；"规则写了但 targets 为空"不算有告警。
6. **容量用数据说话**：并发梯度压测 + Little's Law 验证 + 多 worker 反例，
   结论是单进程写路径 ~94 RPS、瓶颈在 SQLite 写锁（见 `docs/21-并发压测与瓶颈定位.md`）。

详见 `docs/` 各技术文档与 `docs/09-过程审计报告.md`。

---

## 六、交付物清单

- 技术文档 22 份（`docs/`），含压测报告、告警演练记录与面试问答
- 企业级源码 1 套（`zhikong/`），含测试 117 项全绿（1 项因无 API Key 跳过）
- 容器化与部署配置（Dockerfile / docker-compose.yml / docker-compose.monitoring.yml / .env.example）
- 可复现证据（`zhikong/evidence/`）：压测原始数据、告警截图、webhook 送达记录
- 过程审计报告（含审计检查点与埋点对照表）
