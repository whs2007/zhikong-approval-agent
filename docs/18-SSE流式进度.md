# SSE 流式执行进度（补简历缺口）

> 交付时间：2026-11-02
> 动因：简历写了「支持 SSE 流式输出实时推送执行进度」，但仓库里 `grep -r "StreamingResponse"` 为空。
> 这类"写了但没做"是面试最危险的一类——不是能力问题，是诚信问题。本文件记录补齐过程与证据。

---

## 一、为什么不用「轮询」凑数

轮询只能看到**最终状态**，看不到中间过程。而"实时推送执行进度"要表达的是：
analyst 正在分析 → 进入审批闸门 → executor 正在调用工具 → monitor 收敛。

LangGraph 的 `stream(stream_mode="updates")` 每完成一个节点产出一帧，天然对应一个 SSE event，
所以这里做的是**真流式**（节点级），不是"先跑完再逐条播报"。

---

## 二、事件协议

| event | 时机 | data 关键字段 |
|---|---|---|
| `start` | 建流后立即 | `thread_id` `trace_id` `request_type` `amount` |
| `node` | 每完成一个节点 | `node` `elapsed_ms` + 白名单字段（risk_level / execution_result / …） |
| `done` | 图执行完毕 | `paused` `next` `final` `elapsed_ms` `state` |
| `error` | 流内异常 | `message` |

三点设计取舍：

1. **`node` 只外发白名单字段**，不整体外发节点 update。
   节点内部带 `knowledge_context`（企业制度原文）与完整 `analysis_report`，
   进度流只需要"发生了什么"，不该顺手把制度全文推给前端。
2. **`__interrupt__` 跳过**。`updates` 流里 interrupt 会以 `__interrupt__` 这个 key 出现，
   它不是节点、没有状态增量；暂停与否统一由 `done.paused` + `done.next` 表达。
3. **流内异常不抛出**。响应一旦开始发送就无法再改状态码，异常上抛客户端只会看到
   "连接中断"。转成 `error` 事件，前端才能显示原因。

---

## 三、遇到的真实 bug（这段最值得讲）

第一次跑测试，**流能完整发完，但请求最终 500**：

```
ValueError: <Token var=<ContextVar name='zhikong_trace_id' ...>> was created in a different Context
```

### 根因

`bind_trace()` 返回的 `contextvars.Token` 与「创建它的 Context」强绑定。
SSE 的同步生成器由 `iterate_in_threadpool` 驱动，每次 `next()` 可能落在不同的
Context 副本里，退出时 `reset_trace(token)` 就抛 `ValueError`。

### 为什么难发现

日志里 analyst / executor / monitor **全都带着正确的 trace_id**——链路传播是好的，
坏的只有"归还"这一步。所以从日志完全看不出问题，只有断言响应状态码的测试才抓得到。

### 修法

新增 `core.logging.trace_scope()`：不改用 Token，而是「记旧值 → `set()` → 结束时 set 回旧值」。
`ContextVar.set()` 不校验来源 Context，因此跨 Context 迭代也安全。

> 面试可讲：这不是"框架 bug"，是我把为同步请求设计的 API 直接套到流式场景，
> 忽略了 Context 生命周期。同类问题在任何"异步框架 + 同步阻塞库"的组合里都会出现。

---

## 四、证据（真实 curl 输出）

### 4.1 大额 80000 → 停在审批闸门

```
id: 0
event: start
data: {"thread_id":"WF-bf84fac31b1e","trace_id":"037975cd...","request_type":"procurement","amount":80000.0}

id: 1
event: node
data: {"node":"analyst","elapsed_ms":14.1,"risk_level":"high","approval_status":"pending",
       "current_phase":"approve","required_approvers":["部门经理","财务复核","VP"]}

id: 2
event: done
data: {"thread_id":"WF-bf84fac31b1e","paused":true,"next":["approval_gate"],
       "final":"pending_approval","elapsed_ms":36.7,"state":{...}}
```

**读法**：只有 analyst 一帧就 done，`paused=true`、`next=["approval_gate"]`——
说明图在审批闸门真正中断了，而不是"跑完再假装暂停"。

### 4.2 小额 500 → 完整链路（4 帧）

```
event: node  {"node":"analyst","elapsed_ms":13.6,"risk_level":"low","approval_status":"approved"}
event: node  {"node":"executor","elapsed_ms":1064.9,"execution_result":"success"}
event: node  {"node":"monitor","elapsed_ms":1082.3}
event: done  {"paused":false,"final":"success","elapsed_ms":1082.9}
```

**额外信息**：这次 `execution_result` 里带着
`工具: supplier_order（第 3 次尝试成功）`——工具失败率 0.4 的场景下，
指数退避重试真实发生了，流式把这个过程也暴露给了前端。
这是"韧性机制不是纸面配置"的旁证。

### 4.3 `?token=` 兜底（浏览器 EventSource 不能设请求头）

```
$ curl -N -X POST ".../workflow/submit/stream?token=<JWT>" -d '{...}'
id: 0
event: start
data: {"thread_id":"WF-b8fc8d359b08",...}
```

鉴权优先级：有 `Authorization` 头时**只认头**（忽略 query），无头时才用 query。
避免"头有效但 query 是别人令牌"的混淆。

---

## 五、测试（9 例，`tests/test_sse.py`）

| 用例 | 断言什么 | 为什么必须测 |
|---|---|---|
| `test_stream_requires_auth` | 无令牌 → 401 | 流式端点最容易漏鉴权 |
| `test_stream_high_amount_pauses_at_gate` | 首帧 start / 末帧 done / `paused=true` / 无 executor | 抓"退化成非流式"与"中断失效" |
| `test_stream_low_amount_runs_to_completion` | 出现 executor 且 `final∈{success,failed}` | 全链路走通 |
| `test_stream_token_query_fallback` | 仅 `?token=` 也放行 | 前端 EventSource 依赖此分支 |
| `test_stream_rejects_bad_query_token` | 伪 token → 401 | 兜底通道不能变成后门 |
| `test_stream_rejects_empty_detail` | 空详情 → 400 | 校验先于执行 |
| `test_event_ids_are_monotonic` | id 递增且唯一 | 断线后按 Last-Event-ID 续传的前提 |
| `test_stream_writes_workflow_submit_audit` | 审计表有 WORKFLOW_SUBMIT | 防"流式通道绕过审计" |
| `test_two_streams_do_not_share_thread` | 两次提交 thread_id 不同 | 防配置串用 |

全量：**105 passed**（96 + 9），ruff / mypy 全绿。

---

## 六、边界（诚实标注）

1. **只做"提交"的流，没做"审批后恢复"的流**。审批恢复仍是普通 JSON 响应。
   理由：恢复路径已由 `status` 轮询 + 审计覆盖，加流式的边际收益低。
2. **`?token=` 会进 access log**。生产应优先 Bearer 并对日志脱敏；
   短生命周期演示可接受，长期暴露等于把 JWT 写进日志。
3. **无断线续传**。`id` 是递增的、具备续传前提，但未实现 `Last-Event-ID` 重放
   （需要在服务端缓存事件或从 checkpoint 重放，成本高于收益）。
4. **单实例限制**。SSE 连接占用一个线程池 worker（同步生成器），
   高并发下会先打满 threadpool 而不是 CPU；上生产应改 `astream` 全异步。

---

## 七、对简历的影响

| 简历原句 | 现在 |
|---|---|
| 「支持 SSE 流式输出实时推送执行进度」 | ✅ **能撑住了**，可现场 `curl -N` 演示 |

配套的三条简历措辞建议（另两条仍与代码不符）：

- 「FAISS 向量数据库」→ 建议改「向量检索（余弦 + 词项覆盖率混合，可平滑替换 FAISS）」
- 「PostgreSQL」→ 建议改「SQLite（Checkpointer 可切 PostgreSQL）」
- 「图级循环重试最多 3 次」→ 建议改「重试下沉至工具层（指数退避 + 熔断），图级只做终态收敛」
  （这条改动本身是加分故事：发现双层重试会叠加放大调用次数后主动重构）
