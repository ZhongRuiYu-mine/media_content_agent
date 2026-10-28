# coding: utf-8
"""knowledge_store.py - 素材库语义检索工具。

历史外壳随旧路线删除; 现在只保留两个纯函数:
- ``embed_query``: 把查询文本向量化(供素材库检索构造 query 向量);
- ``retrieve``: 余弦相似度 top-k 检索(供需要向量精排的场景复用)。

依赖策略: embedding 走仓库统一 Provider; 检索用 numpy 暴力余弦, 不依赖 faiss;
Provider 不可用时调用方自行降级(取前 top_k)。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def embed_query(embedder, query_text: str):
    """把查询文本向量化(复用 Provider 的 embed_documents 异步接口)。

    返回查询向量(已由 Provider L2 归一化); embedder 不可用/失败返回 None。
    """
    if embedder is None or not query_text:
        return None
    try:
        ed = getattr(embedder, "embed_documents", None)
        if callable(ed):
            out = ed([query_text])
            if hasattr(out, "__await__"):
                out = await out
            if out and out[0] is not None:
                return out[0]
        # 兼容裸 SentenceTransformer(同步 encode)
        enc = getattr(embedder, "encode", None)
        if callable(enc):
            import asyncio
            vec = await asyncio.to_thread(enc, [query_text])
            return vec[0] if len(vec) else None
    except Exception as e:  # noqa: BLE001
        logger.warning("查询向量化失败: %s", e)
    return None


async def retrieve(query_text: str, embedder, vectors, documents, top_k: int = 3) -> list:
    """余弦相似度 top-k 检索; 无向量/Provider/检索失败时降级取前 top_k。"""
    docs = documents or []
    if not docs:
        return []
    top_k = max(1, int(top_k or 1))
    if embedder is None or not vectors or not query_text:
        return docs[:top_k]
    try:
        qvec = await embed_query(embedder, query_text)
        if qvec is None:
            return docs[:top_k]
        import numpy as np
        doc_m = np.asarray(vectors, dtype="float32")
        q_m = np.asarray([qvec], dtype="float32").reshape(1, -1)
        doc_norm = np.linalg.norm(doc_m, axis=1, keepdims=True)
        q_norm = np.linalg.norm(q_m)
        doc_n = doc_m / np.where(doc_norm == 0, 1.0, doc_norm)
        q_n = q_m / (q_norm if q_norm else 1.0)
        sims = (doc_n @ q_n.T).ravel()
        order = np.argsort(-sims)[:top_k]
        return [docs[int(i)] for i in order if 0 <= int(i) < len(docs)]
    except Exception as e:  # noqa: BLE001
        logger.warning("向量检索失败，降级取前 top_k: %s", e)
        return docs[:top_k]
