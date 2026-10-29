# zhikong - 企业级智能工作流平台（源码）

基于 LangGraph 多 Agent 协作的企业级智能工作流平台源码。详见根目录 `../README.md` 与 `../docs/` 技术文档。

## 启动

```bash
pip install -r requirements.txt
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

默认离线配置即可运行；配置 `.env`（参考 `.env.example`）可切换 OpenAI/通义千问 LLM 与 Sentence-Transformers Embedding。

## 分层

- `core/` 配置、JWT 安全、结构化日志
- `services/` 审计、鉴权、知识引擎(RAG)、工作流服务
- `workflows/` 状态定义、可插拔 Provider、Agent 节点、LangGraph 编排
- `api/` 路由、鉴权依赖、应用装配
- `schemas/` / `models/` 数据契约与模型
- `frontend/` 审批看板（原生 SPA）
- `tests/` pytest 用例
