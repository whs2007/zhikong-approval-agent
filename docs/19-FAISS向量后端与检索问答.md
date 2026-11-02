# 19 · FAISS 向量后端与检索面试问答

> 目的：简历写"FAISS 向量库"这件事，从"能说"变成"能证明 + 能被追问"。
> 本文件所有数字都来自本机实测，命令可复现；没有一处是估的。

## 一、改了什么（一句话）

把"写死 numpy 余弦检索"改成**可插拔向量后端**：`faiss`（IndexFlatIP / IndexIVFFlat）
与 `numpy` 两套实现共用同一套融合排序与拒答逻辑，用 `VECTOR_BACKEND` 三态切换，
并把**实际生效的后端**暴露到 `/health` 与 Prometheus 指标。

| 项 | 改前 | 改后 |
|---|---|---|
| 检索实现 | 只有 `NumpyVectorStore` | `NumpyVectorStore` + `FaissVectorStore` |
| 后端选择 | 无法选择 | `VECTOR_BACKEND=auto\|faiss\|numpy` |
| 索引类型 | 无 | 小库 Flat（精确），超阈值自动切 IVF（需训练，不足则回退） |
| 融合/拒答逻辑 | 与 numpy 实现耦合 | 抽为 `_hybrid_rank()`，两后端共用 |
| 可观测 | 无 | `zhikong_vector_backend_info{backend}` / `zhikong_vector_index_vectors` |
| 一致性保障 | 无 | 配 faiss 却没装 → **启动期报错**，不静默降级 |

代码位置：`workflows/providers.py`（`FaissVectorStore` / `_hybrid_rank` /
`_build_inverted_index` / `build_vector_store`）、`core/config.py`（6 个配置项）、
`api/routers/system.py`（`/health`、`/metrics`）。

## 二、为什么是"可插拔"，而不是"直接换掉 numpy"

不是为了炫技，是三条真实约束：

1. **CI 里不一定有 faiss**。`faiss-cpu` 是 18MB 的二进制轮子，某些环境（如离线构建、
   受限镜像）装不上；如果检索硬依赖它，整条流水线会被一个向量库卡死。
2. **"配了却没生效"是最难查的故障**。所以我提供三档而不是一个开关：
   - `auto`：能 import 就用 faiss，否则降级 numpy 并 `warning`；
   - `faiss`：**强制**，缺失直接 `RuntimeError`（宁可启动失败，也不假装在用 FAISS）；
   - `numpy`：强制零依赖，供 CI / 教学 / 排障使用。
3. **降级必须可见**。`auto` 降级虽然不报错，但 `/health` 会明确写 `"vector_backend":"numpy"`，
   指标 `zhikong_vector_backend_info{backend="numpy"} = 1`。**没人观测的降级等于事故**。

## 三、索引类型：为什么默认 Flat，什么时候切 IVF

| 索引 | 是否精确 | 需要训练 | 适用规模 | 本项目是否默认 |
|---|---|---|---|---|
| `IndexFlatIP` | ✅ 精确 | 否 | 数千~数万 | ✅ 默认（`n <= FAISS_FLAT_MAX`） |
| `IndexIVFFlat` | ❌ 近似 | ✅ 需要 | 十万~千万 | 超阈值自动切 |
| `IndexHNSWFlat` | ❌ 近似 | 否（建图） | 百万级、低延迟 | 未用 |

三条判断依据：

- **Flat 是暴力检索，复杂度 O(n·d)**。20000 条 × 256 维 ≈ 5M 次乘加，实测 3.5ms，
  完全够用；在这个量级上引入近似索引，是**用召回损失换用不上的速度**。
- **IVF 必须先训练**（k-means 聚 `nlist` 个簇）。经验下限是每簇约 39 个样本
  （faiss 官方 FAQ 的建议），所以代码里写的是 `nlist = min(FAISS_NLIST, n // 39)`，
  且 `nlist < 4` 时**回退 Flat**——样本不够硬上 IVF，召回会比 Flat 更差。
- **HNSW 建图耗时随 n 增长**，小库没有收益，且删除/更新支持差（本项目启动期一次建库、运行期只读，
  但未来若要热更新，HNSW 会更难改）。

对应代码：`FaissVectorStore._build_index()`；行为由 `tests/test_vector_backend.py::
test_faiss_switches_to_ivf_and_falls_back_when_too_small` 守住。

## 四、实测：**FAISS 在小语料下并不更快**（这条必须记住）

`python scripts/bench_vector_backend.py --sizes 13 1000 5000 20000 --repeat 100`

| 语料条数 | numpy P50 | numpy P95 | faiss P50 | faiss P95 | 索引 | 谁更快 |
|---:|---:|---:|---:|---:|:--|:--|
| 13（本项目真实规模） | 0.047 ms | 0.073 ms | 0.055 ms | 0.083 ms | flat | **numpy** |
| 1 000 | 0.213 ms | 0.277 ms | 0.209 ms | 0.283 ms | flat | 持平 |
| 5 000 | 2.72 ms | 97.4 ms | 0.854 ms | 1.194 ms | flat | faiss |
| 20 000 | 94.5 ms | 103.9 ms | **3.54 ms** | 5.59 ms | flat | faiss（27×） |
| 首查（含建索引）13 条 | 2.3 ms | — | 24.4 ms | — | — | numpy（10×） |

**结论（面试就这么说）**："13 条语料下 FAISS 反而更慢——它的 C++ 调用与线程调度开销
大于一次 13×256 的矩阵乘法。我选它不是为了当前这 13 条，而是为了两件事：
一是规模一旦上万，numpy 的 O(n) 会线性劣化到不可用（20000 条 94ms vs 3.5ms）；
二是我把选择做成了配置项 + 基准脚本，**换不换由数据说话，不由口头结论说话**。
首查成本也是真实代价：24ms 建索引 vs 2.3ms 建矩阵，所以启动期建库、别放到请求路径上。"

> 这段能同时证明三件事：懂原理、做过测量、不吹牛。比"用了 FAISS 性能提升 10 倍"安全得多——
> 后者一旦被问"10 倍是怎么测的"就崩。

## 五、顺带修掉的**真瓶颈**：不在向量检索，在切词

做基准时发现一个反直觉现象：numpy 从 1000 条（7.2ms）到 5000 条（200ms）涨了 28 倍，
但矩阵是线性增长的。于是拆开计时：

```
切一次 query 词:        0.007 ms
全量切 5000 条文档词:   45.3 ms   ← 每次查询都重做
矩阵乘法 (5000, 256):   3.46 ms
```

**词项覆盖率那一层在每次查询时重新切全库文档**，开销是向量检索的 13 倍。
也就是说：这种情况下换 FAISS 优化向量检索，**整体延迟几乎不会变**。

两步修法：

1. 建库期一次性切词（缓存），查询只切 query；
2. 再进一步改成**倒排索引**（`term → [doc_id]`），查询只遍历真正命中的文档，
   复杂度从 O(n) 降到 O(命中数)。

前后对比（同一脚本、同一数据）：

| 规模 | 优化前 numpy | 优化后 numpy | 优化前 faiss | 优化后 faiss |
|---:|---:|---:|---:|---:|
| 1 000 | 7.27 ms | 0.21 ms（35×） | 7.39 ms | 0.21 ms（35×） |
| 5 000 | 100.5 ms | 2.72 ms（37×） | 4.55 ms | 0.85 ms（5.4×） |
| 20 000 | 200.3 ms | 94.5 ms（2.1×） | 31.3 ms | 3.54 ms（8.8×） |

**这段是整份文档里最值钱的部分**：它证明"先测量再优化"，而不是"听说 FAISS 快就上 FAISS"。

## 六、证据（可复现）

```bash
# 1) 后端真实生效（auto 时按依赖决定）
curl -s localhost:8000/api/v1/health | python3 -m json.tool
#   "knowledge_chunks": 13, "vector_backend": "faiss"

# 2) Prometheus 指标
curl -s -H "Authorization: Bearer $METRICS_TOKEN" localhost:8000/api/v1/metrics \
  | grep -E "vector_backend_info|vector_index_vectors"
#   zhikong_vector_backend_info{backend="faiss"} 1.0
#   zhikong_vector_backend_info{backend="numpy"} 0.0
#   zhikong_vector_index_vectors 13.0

# 3) 三态对照（实测输出）
VECTOR_BACKEND=numpy python -c "...build_vector_store().backend"   # → numpy
VECTOR_BACKEND=faiss python -c "sys.modules['faiss']=None; ..."
#   → RuntimeError: VECTOR_BACKEND=faiss 但未安装 faiss-cpu；请 `pip install faiss-cpu` ...
VECTOR_BACKEND=auto  python -c "sys.modules['faiss']=None; ..."   # → numpy（降级不报错）

# 4) 单元测试（12 例，含"两后端排序完全一致"与"无关查询都拒答"）
python -m pytest tests/test_vector_backend.py -q
```

## 七、面试追问 16 题（按被问概率排序）

### A. 原理类

**1. 为什么用内积（IP）不用 L2？**
> 要点：我的 embedding 输出已做 L2 归一化（`LocalEmbedding.embed` 里 `vec /= norm`），
> 此时 `‖a-b‖² = 2 - 2·a·b`，**内积排序等价于欧氏距离排序**，而内积省一次开方/减法。
> 危险回答："IP 就是余弦"——不归一化时 IP 受向量长度影响，不等价。

**2. 余弦相似度为什么适合文本？**
> 要点：只看方向不看长度，避免"长文档因为词多而分数虚高"。短查询与长条款长度差异大，
> 用 L2 会被长度主导。

**3. IVF 的原理？`nlist` 和 `nprobe` 分别是什么？**
> 要点：先用 k-means 把向量聚成 `nlist` 个簇（粗量化），查询时只搜最近的 `nprobe` 个簇。
> `nlist` 越大，单簇越小、建索引越慢、训练样本要求越高；`nprobe` 越大越准越慢，
> 是**召回率与延迟的直接旋钮**。本项目 `nlist = min(64, n//39)`、`nprobe = min(8, nlist)`。

**4. IVF 为什么需要训练？训练集要多少？**
> 要点：k-means 要有样本才能定簇心。经验下限每簇 ~39 个样本（faiss FAQ），
> 所以 `n < 39·nlist` 时训练出来的簇没有代表性，召回会掉。代码里不足就回退 Flat。

**5. HNSW 是什么？为什么没用？**
> 要点：分层可导航小世界图，查询是图上的贪心游走，复杂度近似 O(log n)，
> 召回高、无需训练，但**建图慢、内存大、删除/更新支持差**。本项目规模用不上，
> 且未来若要热更新索引会更麻烦。

**6. PQ（乘积量化）解决什么问题？**
> 要点：压缩向量、降内存。把 d 维切成 m 段，每段用 256 个聚类中心编码成 1 字节，
> 内存降到 1/m，代价是有损、需要训练。百万级 + 内存受限才值得。

**7. faiss 返回的 id 是什么？能直接当业务主键吗？**
> 要点：**不能**。`IndexFlat` 返回的是向量加入顺序的**内部位置**（0..n-1），
> 删除/重建后就会错位。业务 id 要么用 `IndexIDMap` 包一层存自定义 id，
> 要么像本项目一样自己维护 `texts[i] / meta[i]` 的平行数组——**位置即下标，重建时整体替换**。

### B. 设计取舍类

**8. 13 条语料为什么也要上 FAISS？是不是过度设计？**
> 要点：**是，纯性能角度是过度设计**（实测小库 numpy 更快）。我的处理是把选择权做成配置 +
> 基准脚本：默认 `auto`，真实规模下它不会带来收益，但规模上万时不需要改代码就能切过去。
> 如果面试官继续压："那你会不会直接用 numpy？"——答"会，而且我的默认配置在
> 无 faiss 环境下就是 numpy，功能完全一致"。

**9. 为什么两个后端要共用一套融合逻辑？**
> 要点：否则"换后端"会悄悄改变排序与拒答行为，等于换了个 bug。共用 `_hybrid_rank()`
> 后，测试可以直接断言"两后端 top-k 序列完全相同"，这是可插拔的验收条件。

**10. 混合检索里的"词项覆盖率"具体怎么算？为什么不用 BM25？**
> 要点：查询切词（中文 bigram + 英文/数字按词），覆盖率 = 命中词数 / 查询词总数，
> 是**绝对量纲**（0~1 有物理意义）。没用 BM25 是因为：语料仅 13 块，IDF 统计没有意义
> （几乎每个词都只出现在 1~2 个文档里），BM25 的参数（k1/b）在这么小的库上纯属噪声。
> 规模上去后应该换 BM25 或学习型稀疏检索（SPLADE）。**这条回答能证明我知道 BM25 是什么，
> 也知道它什么时候不该用。**

**11. 相关度阈值为什么必须用"绝对量纲"？**
> 要点：因为融合前做了 min-max 归一化，归一化是**相对本次查询**做的——top-1 恒为 1.0，
> 拿它当门槛等于没门槛（任何查询都能过）。所以门槛用覆盖率（绝对量纲）。
> 这是"归一化会破坏绝对可比性"的经典坑。

**12. 换了 embedding 模型怎么办？**
> 要点：索引里存的是旧模型的向量，维度或语义空间一变，混用就是错配。
> 代码用 `_embedder_id = 类型:维度` 检测，变化即**整体重建**；生产上还应带
> 模型版本号 + 索引版本号，做双写切换而不是原地重建。

### C. 边界与质疑类（最容易被问倒，必须背熟）

**13. 「这是你自己做的吗？」**
> 要点：**不要含糊**。诚实版："项目主体是我在已有代码上迭代的，FAISS 这一层是我最近补的：
> 我把原来写死的 numpy 检索抽成可插拔后端，加了 Flat/IVF 的规模策略、降级可见性和
> 12 条测试，并用基准脚本测出'小库 FAISS 更慢'这个反直觉结论。"
> 这个回答比"全是我写的"安全，也比"我什么都没做"值钱——**它展示的是判断力，不是苦劳**。

**14. 你怎么知道检索效果好？有没有 recall@k？**
> 要点：**没有严格的 recall@k**（没有人工标注的相关性金标集），我只有 19 条
> 端到端评测用例（risk/injection/retrieval 三类），其中 retrieval 10 条验的是
> "该命中的条款是否进 top-k"和"无关查询是否拒答"。
> 这是明确的不足：正确做法是标 200~500 条 query-doc 相关性，算 recall@k / MRR。
> **主动承认比被问出来好。**

**15. 单机能扛多少 QPS？索引能热更新吗？**
> 要点：**没压测过，答不出数字**（这是已知缺口）。结构上的事实是：索引在启动期构建、
> 运行期只读，faiss 的 `search` 是线程安全的（`add` 不是）；热更新目前是整体重建
> （`reload_knowledge` 换对象引用），13 条语料成本可忽略，上万条就会卡住请求。
> 生产做法：索引分片 + 双缓冲切换 + 后台重建。

**16. 为什么不用 pgvector / Milvus？**
> 要点：当前规模下它们都是净成本：pgvector 要多一套 PG 运维（本项目 auth/audit 还是 SQLite），
> Milvus 是独立集群。**选型要看规模与团队运维能力，不是看谁更"先进"。**
> 迁移触发条件我会写成："语料 > 10 万 或 需要多副本/在线更新 或 需要按元数据强过滤"。

## 八、诚实边界（写进 KNOWN_LIMITATIONS 同步）

- 语料仅 13 块，**FAISS 在当前规模不带来性能收益**（实测更慢），价值在"可扩展 + 可切换"。
- **无 recall@k 金标集**，只有 19 条端到端用例；检索质量缺少严格量化。
- 索引**未做持久化**（`FAISS_INDEX_PATH` 已预留但未实现 `write_index/read_index`），
  重启靠重新 embed 13 条文本（毫秒级）；上万条必须补持久化。
- 单进程、无并发压测；`search` 线程安全但整体 QPS 上限未知。
- IVF 路径只在单测里用合成数据验证过（240 条切 IVF、60 条回退 Flat），**没有真实大规模数据验证**。
