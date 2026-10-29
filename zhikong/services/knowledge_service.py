"""知识引擎服务（RAG 驱动决策的知识侧）。

- 加载 data/knowledge/ 下文档，分块后向量化入库。
- 检索 Top-K 相关规则并格式化为上下文，供 Analyst 节点注入决策。
- 规则更新只需替换文档并 reload，无需改代码（对应教材难点 3）。
"""
from __future__ import annotations

import os
import re

from core.config import KNOWLEDGE_DIR, settings
from core.logging import logger
from workflows.providers import VectorStore, build_embedding, build_vector_store

_EMBED = None
_STORE: VectorStore | None = None
_DOC_COUNT = 0


def _get_embedder():
    global _EMBED
    if _EMBED is None:
        _EMBED = build_embedding()
    return _EMBED


def _split(text: str, size: int = 500, overlap: int = 50) -> list[str]:
    # 按标题分段优先，再按长度切分
    parts = re.split(r"\n#+\s", text)
    chunks: list[str] = []
    for raw in parts:
        part = raw.strip()
        if not part:
            continue
        if len(part) <= size:
            chunks.append(part)
        else:
            for i in range(0, len(part), size - overlap):
                chunks.append(part[i:i + size])
    return chunks


def build() -> int:
    """加载并构建知识库，返回文档块数量。"""
    global _STORE, _DOC_COUNT
    store = build_vector_store()
    count = 0
    if not os.path.isdir(KNOWLEDGE_DIR):
        logger.warning(f"[knowledge] 目录不存在: {KNOWLEDGE_DIR}")
    for root, _, files in os.walk(KNOWLEDGE_DIR):
        for name in files:
            if not name.endswith((".txt", ".md")):
                continue
            path = os.path.join(root, name)
            try:
                with open(path, encoding="utf-8") as f:
                    text = f.read().strip()
            except Exception as e:
                logger.warning(f"[knowledge] 读取失败 {path}: {e}")
                continue
            title = os.path.splitext(name)[0]
            for chunk in _split(text):
                store.add([{"content": chunk, "title": title, "source": path}])
                count += 1
    _STORE = store
    _DOC_COUNT = count
    logger.info(f"[knowledge] 知识库就绪: {count} 个文档块")
    return count


def _ensure_built() -> VectorStore:
    if _STORE is None:
        build()
    assert _STORE is not None
    return _STORE


def search(query: str, top_k: int | None = None) -> str:
    """检索相关规则并拼为上下文字符串。"""
    top_k = top_k or settings.TOP_K
    results = _ensure_built().search(query, top_k, _get_embedder())
    if not results:
        return "（未检索到相关规则）"
    blocks = []
    for i, r in enumerate(results, 1):
        blocks.append(f"【规则{i}】{r.get('title','')}\n{r['content']}\n(相似度:{r.get('score',0):.3f})")
    return "\n\n".join(blocks)


def chunk_count() -> int:
    return _DOC_COUNT
