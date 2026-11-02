# 20 · PostgreSQL Checkpointer 验证（跨进程恢复）

> 简历写"PostgreSQL"。本文件的立场是：**不说"用了 PG 做业务库"（那是假的），只说
> "Checkpointer 已验证可切 PG 并跨进程恢复"（这是真的，且能当场复现）**。

## 一、范围（先把边界钉死）

| 组件 | 默认 | 本次验证 | 说明 |
|---|---|---|---|
| Checkpointer（工作流状态） | SQLite 文件 | ✅ **PostgreSQL** | 本文件验证的部分 |
| auth / audit 业务库 | SQLite | ❌ 仍是 SQLite | 未迁移，见「边界」 |
| 知识库向量 | 内存（FAISS/numpy） | ❌ 未持久化 | 启动期重建 |

**结论一句话**：`CHECKPOINTER=postgres` 这条路是**通的、被验证过的**；
但"整个系统跑在 PG 上"**不成立**，多副本一致性仍不具备。

## 二、做了什么

| 文件 | 作用 |
|---|---|
| `workflows/graph.py` | 修一个真 bug（见第四节）；`CHECKPOINTER=postgres` 走 `psycopg.Connection` 直连并保活 |
| `docker-compose.postgres.yml` | postgres:16-alpine（宿主 15432，避免占用本机 5432）+ app，带 healthcheck |
| `requirements-postgres.txt` | 可选依赖（`langgraph-checkpoint-postgres` + `psycopg[binary]`），刻意不塞进主依赖 |
| `scripts/verify_pg_checkpointer.py` | 两步式复现脚本（`--submit` / `--resume`），**必须两个进程**才有说服力 |

## 三、验证记录（真实输出）

### 1) 建表：`saver.setup()` 幂等创建 4 张表

```
 public | checkpoint_blobs        | table
 public | checkpoint_migrations   | table
 public | checkpoint_writes       | table
 public | checkpoints             | table
```

### 2) 提交 → 暂停（写入 PG）

```
提交完成 thread=WF-6e3e62183413 phase=approve approval=pending paused=True
```

```
 thread_id        | checkpoints | latest
------------------+-------------+--------------------------------------
 WF-253da05b0ac1  |           3 | 1f1d6d79-...
```

### 3) **杀进程** → 全新进程从 PG 读回

HTTP 侧验证（`kill -9` 后用新进程起服务）：

```
重启后：thread=WF-253da05b0ac1 | phase=approve | approval=pending | amount=88000.0
待审批列表：条数=1 | WF-253da05b0ac1 | 金额 88000.0 | 等待h 0.05
```

脚本侧验证（进程 A 提交 → 进程 B 恢复）：

```
[进程A] 提交完成 thread=WF-6e3e62183413 phase=approve approval=pending
[进程B] [恢复前] phase=approve approval=pending amount=88000.0
[进程B] [恢复后] phase=done approval=approved exec=success
```

### 4) 恢复后落库继续增长

```
 thread_id        | checkpoints | 说明
------------------+-------------+------------------
 WF-253da05b0ac1  |           6 | 暂停时 3 条，审批恢复后新增 3 条
 checkpoint_blobs = 8   checkpoint_writes = 42
```

## 四、踩到的真 bug（这条比"接通了"值钱）

第一次按最简写法接：

```python
saver = PostgresSaver.from_conn_string(settings.POSTGRES_URL).__enter__()
saver.setup()
```

启动直接崩：

```
psycopg.OperationalError: the connection is closed
  at langgraph/checkpoint/postgres/__init__.py:97 in setup -> self._cursor()
```

**根因**：`from_conn_string()` 是 `@contextmanager`（内部 `with Connection.connect(...)`）。
只调 `.__enter__()` 却**不保留那个生成器对象**，它立刻成为垃圾被 GC 回收；
GC 触发 `GeneratorExit` → 走 `with` 的 `finally` → **连接被关闭**。
于是拿到手的 `saver` 里是一个已关闭的连接。

**修法**：自己创建连接并显式保活，不经由 contextmanager：

```python
conn = Connection.connect(settings.POSTGRES_URL, autocommit=True,
                          prepare_threshold=0, row_factory=dict_row)
_KEEPALIVE["pg_conn"] = conn          # 模块级引用，防止被 GC
saver = PostgresSaver(conn)
saver.setup()
```

**为什么值得讲**：这是一个"语法完全合法、类型检查也过、运行时报连接已关闭"的坑，
根因在**对象生命周期**而不是 SQL 或配置。面试里说清"contextmanager + `__enter__()`
不保留引用会被 GC"，比背 PG 参数更能证明你真的调过代码。

## 五、边界（上生产前必须补）

| 缺口 | 现状 | 上生产前必须做什么 |
|---|---|---|
| **业务库仍是 SQLite** | auth/audit 写本地文件 | 迁 PG 或改用统一 ORM；否则多副本时"状态在 PG、审计在各自本地" |
| **单节点 PG** | docker 单实例，无副本 | 主从/托管实例 + 备份 + PITR；`checkpoint_*` 表纳入备份范围 |
| **无连接池** | 每个进程一条长连接 | 用 `psycopg_pool.ConnectionPool`；当前并发低，连接数是进程数 |
| **无迁移管理** | 依赖 `saver.setup()` 幂等建表 | 引入 alembic / 版本化迁移；`checkpoint_migrations` 表已由库自管 |
| **未做故障注入** | 只验证"正常杀进程" | 应验证：PG 重启、网络闪断、事务回滚时的行为 |
| **无压测** | 不知道 checkpoint 写入的吞吐上限 | 每步工作流写 1~N 行，需测 QPS 与表膨胀（VACUUM 策略） |

## 六、面试问答（8 题）

**1. 为什么 Checkpointer 要换 PG，SQLite 不行吗？**
> SQLite 是**单机文件锁**：多副本部署时，A 副本写的状态 B 副本看不见；
> 且写并发上来会 `database is locked`。PG 提供跨进程/跨机器的共享状态与行级并发。
> 但我不会说"SQLite 不行"——**单副本场景它是正确选择**（零运维、事务完整），
> 这就是为什么默认仍是 SQLite。

**2. 恢复的原理是什么？**
> 每执行完一个节点，LangGraph 把**状态快照**（channels 的值）写成一条 checkpoint
> （`checkpoints` + `checkpoint_blobs`），`interrupt` 时快照里记录 `next=[approval_gate]`。
> 恢复 = 用同一 `thread_id` 读最新 checkpoint，然后 `Command(resume=...)` 从 `next` 继续。
> 关键点：**状态全在 DB 里，进程可以是全新的**——我的脚本就是两个独立进程。

**3. 那三张表分别存什么？**
> `checkpoints`：每条快照的元信息（thread_id、checkpoint_id、parent、step）；
> `checkpoint_blobs`：大块的 channel 值（按 channel + version 分块存）；
> `checkpoint_writes`：节点执行过程中的中间写入（用于并发/重放）。
> 设计上是 **copy-on-write**：只写变化的 channel，不是每次全量快照。

**4. checkpoint 会不会无限膨胀？**
> 会。每条快照都留就是 O(步数)。生产要做保留策略：按 thread 保留最近 N 条，
> 或按时间 TTL 归档；PG 侧还要注意 autovacuum 与表膨胀。本项目**没做**，是已知缺口。

**5. 多副本同时审批同一个 thread 会怎样？**
> 业务层有幂等/状态校验（状态不是 pending 就拒绝），但**跨副本存在竞态**：
> 两个副本同时读到 pending 都可能放行。真正的解法是 DB 级乐观锁
> （`UPDATE ... WHERE state='pending'` 影响行数判断）或 `SELECT FOR UPDATE`。
> 当前是单副本，所以没暴露——**这是"多副本前必须补"的第一项**。

**6. 为什么 pg 依赖不放进 requirements.txt？**
> 默认部署不需要（SQLite 够用），而 psycopg 带二进制轮子会拖慢 CI 安装。
> 这是**刻意的可选化**：`requirements-postgres.txt` 单独装，配置文件里也有明确报错
> （缺依赖时抛 RuntimeError 而不是静默回退 SQLite）。

**7. 你怎么证明"真的跨进程了"，而不是同一个进程里换个连接？**
> `scripts/verify_pg_checkpointer.py` 设计成两步：`--submit` 结束后进程退出，
> `--resume` 是**另一个进程**（新 PID、新解释器、新连接池）。
> 另外还做了 `kill -9` 后重启 HTTP 服务的验证——`kill -9` 没有任何机会优雅落盘，
> 能读回来只可能是 DB 里有。

**8. 这套能直接上生产吗？**
> **不能**。它证明的是"这条路通"，不是"这套能扛"。缺的是：业务库迁移、
> 连接池、迁移管理、多副本并发控制、备份与 PITR、压测。这些我写在
> `KNOWN_LIMITATIONS.md` 里，不打算用"已经接了 PG"含混过去。

## 七、复现命令

```bash
# 1) 起库（宿主 15432）
docker compose -f docker-compose.postgres.yml up -d postgres

# 2) 装可选依赖
pip install -r requirements-postgres.txt

# 3) 两句验证（两个进程）
export ENV=dev TOOL_FAILURE_RATE=0 \
       CHECKPOINTER=postgres \
       POSTGRES_URL=postgresql://zhikong:zhikong_dev_pw@127.0.0.1:15432/zhikong
python scripts/verify_pg_checkpointer.py --submit
python scripts/verify_pg_checkpointer.py --resume <上一步输出的 thread_id>

# 4) 看落库
docker exec zhikong-pg psql -U zhikong -d zhikong \
  -c "select thread_id, count(*) from checkpoints group by thread_id;"
```
