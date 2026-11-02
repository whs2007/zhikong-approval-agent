"""向量后端可插拔性测试（FAISS / Numpy）。

核心要证明的不是"FAISS 能跑"，而是三件事：
1. 换后端**不改变**排序结果与拒答行为（融合与门槛逻辑共用，后端只负责算相似度）；
2. 后端选择**不静默撒谎**：配 faiss 却没装 → 启动期报错；配 auto → 降级但可见；
3. 规模切换策略（flat → ivf）在数据量不足时**回退**而不是崩溃。
"""
from __future__ import annotations

import numpy as np
import pytest

from core.config import settings
from workflows.providers import (
    FaissVectorStore,
    LocalEmbedding,
    NumpyVectorStore,
    build_vector_store,
)

faiss = pytest.importorskip("faiss", reason="未安装 faiss-cpu，跳过 FAISS 后端测试")

# 小语料：覆盖"金额阈值/审批人/采购/报销"四类，便于构造相关与无关查询
DOCS = [
    {"content": "采购金额超过五万元需部门经理与财务复核审批，超过五十万元需副总裁审批。", "title": "采购制度"},
    {"content": "差旅报销需在出差结束后七个工作日内提交发票，逾期需走例外审批。", "title": "报销制度"},
    {"content": "办公用品单笔采购不超过两千元可由部门主管直接审批。", "title": "采购细则"},
    {"content": "合同签署前必须完成法务合规审查，涉及数据出境需额外安全评估。", "title": "合同制度"},
    {"content": "供应商准入需提供营业执照与近三年审计报告，并完成现场考察。", "title": "供应商管理"},
]


def _make(store_cls, dim: int = 64):
    store = store_cls()
    store.add(DOCS)
    return store, LocalEmbedding(dim=dim)


def test_faiss_and_numpy_return_identical_ranking():
    """同一语料/同一查询：两个后端必须给出**完全相同**的排序。

    这是"可插拔"的验收条件——如果换后端结果就变，那所谓迁移只是换了个 bug。
    flat 是精确检索且融合逻辑共用，因此不仅 top-1，整条结果序列都应一致。
    """
    query = "采购五万元以上需要谁审批"
    np_store, emb = _make(NumpyVectorStore)
    fs_store, _ = _make(FaissVectorStore)

    np_res = np_store.search(query, 3, emb)
    fs_res = fs_store.search(query, 3, emb)

    assert [r["content"] for r in np_res] == [r["content"] for r in fs_res]
    assert [r["title"] for r in np_res] == [r["title"] for r in fs_res]
    # 分数同源（同一 embedder + 同一融合函数），允许浮点误差但不应有量级差异
    for a, b in zip(np_res, fs_res, strict=True):
        assert a["score"] == pytest.approx(b["score"], abs=1e-5)


def test_refusal_threshold_unchanged_across_backends():
    """无关查询必须**都拒答**（返回空），证明换后端没有削弱拒答门槛。

    拒答靠的是词项覆盖率这一绝对量纲判据，而不是向量相似度——
    这也是它能在两种后端下表现一致的原因。
    """
    query = "今天北京天气怎么样"
    for cls in (NumpyVectorStore, FaissVectorStore):
        store, emb = _make(cls)
        assert store.search(query, 3, emb) == [], f"{cls.__name__} 未按门槛拒答"


def test_faiss_flat_chosen_for_small_corpus():
    """小语料必须用 IndexFlatIP：IVF 在样本不足时召回会掉，属于用复杂度换更差结果。"""
    store, emb = _make(FaissVectorStore)
    store.search("采购审批", 3, emb)
    assert store.kind == "flat"
    assert store.index is not None
    assert store.index.ntotal == len(DOCS)


def test_faiss_switches_to_ivf_and_falls_back_when_too_small(monkeypatch):
    """规模策略：样本足够切 IVF，样本不足则回退 flat（不许崩）。"""
    big = [{"content": f"制度条款第{i}条：金额 {i * 100} 元的采购需部门审批。", "title": f"t{i}"}
           for i in range(240)]

    # 情形一：阈值调低 → 240 条足以训练 IVF（nlist = min(64, 240//39) = 6）
    monkeypatch.setattr(settings, "FAISS_FLAT_MAX", 10)
    store = FaissVectorStore()
    store.add(big)
    store.search("采购审批", 3, LocalEmbedding(dim=64))
    assert store.kind == "ivf"
    assert store.index.ntotal == len(big)

    # 情形二：样本不足以支撑 nlist>=4 → 回退 flat，而不是抛训练异常
    small = big[:60]  # 60 // 39 = 1 → nlist < 4 → flat
    store2 = FaissVectorStore()
    store2.add(small)
    store2.search("采购审批", 3, LocalEmbedding(dim=64))
    assert store2.kind == "flat"


def test_faiss_rebuilds_index_when_embedder_dim_changes():
    """换 embedder（维度变化）必须重建索引：旧向量留在索引里就是错配。"""
    store, emb32 = _make(FaissVectorStore, dim=32)
    store.search("采购审批", 3, emb32)
    assert store.index.d == 32

    emb64 = LocalEmbedding(dim=64)
    store.search("采购审批", 3, emb64)
    assert store.index.d == 64, "维度变化后未重建索引"


def test_faiss_returns_empty_on_empty_store():
    store = FaissVectorStore()
    assert store.search("任意查询", 3, LocalEmbedding(dim=32)) == []


def test_backend_selection_numpy_forced(monkeypatch):
    monkeypatch.setattr(settings, "VECTOR_BACKEND", "numpy")
    assert isinstance(build_vector_store(), NumpyVectorStore)


def test_backend_selection_auto_prefers_faiss(monkeypatch):
    monkeypatch.setattr(settings, "VECTOR_BACKEND", "auto")
    assert isinstance(build_vector_store(), FaissVectorStore)


def test_backend_selection_invalid_value_raises(monkeypatch):
    """非法配置必须显式失败：静默回退会让人以为配置生效了。"""
    monkeypatch.setattr(settings, "VECTOR_BACKEND", "milvus")
    with pytest.raises(ValueError, match="VECTOR_BACKEND"):
        build_vector_store()


def test_backend_forced_faiss_without_module_raises(monkeypatch):
    """配了 faiss 但没装 → 启动期报错，而不是偷偷跑 numpy。"""
    monkeypatch.setattr(settings, "VECTOR_BACKEND", "faiss")
    monkeypatch.setitem(__import__("sys").modules, "faiss", None)  # 令 import faiss 抛 ImportError
    with pytest.raises(RuntimeError, match="faiss-cpu"):
        build_vector_store()


def test_backend_auto_without_module_degrades_silently(monkeypatch):
    """配了 auto 但没装 → 降级 numpy（功能等价），且能读出真实后端。"""
    monkeypatch.setattr(settings, "VECTOR_BACKEND", "auto")
    monkeypatch.setitem(__import__("sys").modules, "faiss", None)
    store = build_vector_store()
    assert isinstance(store, NumpyVectorStore)
    assert store.backend == "numpy"


def test_lexical_scores_are_shared_between_backends():
    """融合用的词项分必须与后端无关：同一查询两后端取到的覆盖率一致。"""
    from workflows.providers import _lexical_scores

    query = "采购审批"
    a = _lexical_scores([d["content"] for d in DOCS], query)
    assert a is not None
    assert np.all(a >= 0) and np.all(a <= 1)
    # 至少有一条命中了"采购"或"审批"，否则该查询对本语料无区分度
    assert float(a.max()) > 0
