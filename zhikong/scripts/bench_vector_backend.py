"""向量后端性能对比（numpy 暴力检索 vs FAISS IndexFlatIP）。

为什么必须测：FAISS 不是"用了就快"。小语料下它是 C++ 调用 + 可能的线程调度开销，
numpy 的 BLAS 矩阵乘法反而更快。不做这条曲线就敢在面试里说"用 FAISS 提升性能"，
被追问"提升多少"就答不上来。

用法：
    python scripts/bench_vector_backend.py                # 默认 100/1000/5000/20000
    python scripts/bench_vector_backend.py --sizes 13 1000 --repeat 200

输出：markdown 表格（可直接贴进文档/面试材料）。
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import settings  # noqa: E402
from workflows.providers import (  # noqa: E402
    FaissVectorStore,
    LocalEmbedding,
    NumpyVectorStore,
)

_TOPICS = ["采购金额", "差旅报销", "合同签署", "供应商准入", "数据出境",
           "办公用品", "发票", "法务审查", "审批权限", "预算科目"]


def _corpus(n: int) -> list[dict]:
    """合成语料：主题词循环拼接，保证既有区分度又不至于全同（全同会让索引退化成常数）。"""
    docs = []
    for i in range(n):
        t = _TOPICS[i % len(_TOPICS)]
        docs.append({
            "content": f"制度条款{i}：涉及{t}的第{i % 97}条规定，金额 {i * 13 % 50000} 元需"
                       f"{'部门经理' if i % 3 else '财务复核'}审批，并在 {i % 30 + 1} 个工作日内完成。",
            "title": f"doc{i}",
        })
    return docs


def _queries(n: int) -> list[str]:
    return [f"{_TOPICS[i % len(_TOPICS)]}超过五万元需要谁审批" for i in range(n)]


def _time_search(store, emb, queries, top_k: int) -> tuple[float, float, float]:
    """返回 (p50, p95, 平均) 毫秒。先做一次预热，避免把首次建索引算进检索耗时。"""
    store.search(queries[0], top_k, emb)
    samples = []
    for q in queries:
        t0 = time.perf_counter()
        store.search(q, top_k, emb)
        samples.append((time.perf_counter() - t0) * 1000)
    samples.sort()
    return (statistics.median(samples),
            samples[min(len(samples) - 1, int(len(samples) * 0.95))],
            statistics.fmean(samples))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--sizes", nargs="+", type=int, default=[13, 100, 1000, 5000, 20000])
    p.add_argument("--repeat", type=int, default=100, help="每个规模执行的查询次数")
    p.add_argument("--top-k", type=int, default=3)
    args = p.parse_args()

    emb = LocalEmbedding(dim=settings.EMBED_DIM)
    queries = _queries(args.repeat)

    print("| 语料条数 | numpy P50 (ms) | numpy P95 (ms) | faiss P50 (ms) | faiss P95 (ms) "
          "| faiss 索引类型 | 谁更快 |")
    print("| ---: | ---: | ---: | ---: | ---: | :-- | :-- |")
    for n in args.sizes:
        docs = _corpus(n)

        np_store = NumpyVectorStore()
        np_store.add(docs)
        t0 = time.perf_counter()
        np_store.search(queries[0], args.top_k, emb)  # 触发建矩阵
        np_build = (time.perf_counter() - t0) * 1000
        np50, np95, _ = _time_search(np_store, emb, queries, args.top_k)

        fs_store = FaissVectorStore()
        fs_store.add(docs)
        t0 = time.perf_counter()
        fs_store.search(queries[0], args.top_k, emb)  # 触发建索引
        fs_build = (time.perf_counter() - t0) * 1000
        fs50, fs95, _ = _time_search(fs_store, emb, queries, args.top_k)

        winner = "numpy" if np50 < fs50 else "faiss"
        print(f"| {n} | {np50:.3f} | {np95:.3f} | {fs50:.3f} | {fs95:.3f} "
              f"| {fs_store.kind} | {winner} |")
        print(f"|   ↳ 首查耗时（含建索引） | {np_build:.1f} | — | {fs_build:.1f} | — | — | — |")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
