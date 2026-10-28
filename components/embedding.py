# coding: utf-8
"""components/embedding.py — 向量客户端(仅运行于 jiWen 解释器 D:\\jiuwen\\Scripts\\python, 复用 openjiuwen)。

设计(design/05 §4.5): embedding 一律走 **openjiuwen 提供的 Provider**:
- 首选: `openjiuwen.core.memory.lite.embeddings.OpenAICompatibleEmbeddingProvider`
  (OpenAI 兼容 `/embeddings` 端点, 配置 env EMBEDDING_MODEL_NAME/BASE_URL/API_KEY)。
  本地部署的 Qwen3-Embedding-8B(./model)由你侧以任意 OpenAI 兼容服务暴露该端点后即可用;
- 其次: 本解释器若自带 sentence_transformers 且配置 EMBEDDING_LOCAL_DIR, 才允许进程内加载;
- 服务不可用 → available()=False, 调用方(分类器)降级为规则+用户裁决, 不静默调云 LLM。

本模块只在必要时做两层薄封装(向量 L2 归一化 + cosine), 编解码全交给 openjiuwen。
"""
from __future__ import annotations

import logging
import math
import os
from typing import List, Optional

logger = logging.getLogger(__name__)


def _l2_normalize(vec: List[float]) -> List[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if not norm or norm <= 0:
        return vec
    return [x / norm for x in vec]


class EmbeddingProvider:
    """embedding 抽象(测试可注入 FakeEmbedding 子类)。"""

    def available(self) -> bool:
        raise NotImplementedError

    async def embed_documents(self, texts: List[str]) -> Optional[List[List[float]]]:
        """返回已 L2 归一化的向量; None = 服务不可用。"""
        raise NotImplementedError

    def cosine(self, a: List[float], b: List[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        return sum(x * y for x, y in zip(a, b))


class NullEmbeddingProvider(EmbeddingProvider):
    """未配置端点时的占位, available()=False。"""

    def available(self) -> bool:
        return False

    async def embed_documents(self, texts: List[str]) -> Optional[List[List[float]]]:
        return None


class OpenJiwenEmbeddingProvider(EmbeddingProvider):
    """薄封装 openjiuwen 的 OpenAICompatibleEmbeddingProvider(异步, 归一化)。"""

    def __init__(self, base_url: str, model: str, api_key: str = "EMPTY"):
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self._inner = None
        self._ok: Optional[bool] = None

    def _get_inner(self):
        if self._inner is None:
            from openjiuwen.core.memory.lite.embeddings import (  # noqa: PLC0415
                OpenAICompatibleEmbeddingProvider,
            )
            self._inner = OpenAICompatibleEmbeddingProvider(
                api_key=self.api_key or "EMPTY",
                model=self.model,
                base_url=self.base_url,
            )
        return self._inner

    def available(self) -> bool:
        return True  # 端点已配置; 真实可达性由 embed 探活决定(None 表示不可用)

    async def embed_documents(self, texts: List[str]) -> Optional[List[List[float]]]:
        if not texts:
            return []
        if self._ok is False:
            return None
        try:
            inner = self._get_inner()
            vecs = await inner.embed_documents(texts)
            if not vecs:
                self._ok = False
                return None
            return [_l2_normalize(v) for v in vecs]
        except Exception as e:  # noqa: BLE001
            logger.warning("openjiuwen embedding 调用失败: %s", e)
            self._ok = False
            return None


class LocalSentenceTransformersProvider(EmbeddingProvider):
    """进程内本地加载(modelscope 下载的 ./model, 需本解释器装有 sentence_transformers+torch)。"""

    def __init__(self, model_path: str, batch_size: int = 8):
        self.model_path = model_path
        self.batch_size = int(batch_size)
        self._model = None
        self._err: Optional[str] = None

    def _load(self):
        if self._model is None and self._err is None:
            try:
                from sentence_transformers import SentenceTransformer  # noqa: PLC0415
                self._model = SentenceTransformer(self.model_path)
                logger.info("本地 embedding 加载成功: %s", self.model_path)
            except Exception as e:  # noqa: BLE001
                self._err = str(e)
                logger.error("本地 embedding 加载失败: %s", e)

    def available(self) -> bool:
        self._load()
        return self._model is not None

    async def embed_documents(self, texts: List[str]) -> Optional[List[List[float]]]:
        if not texts:
            return []
        self._load()
        if self._model is None:
            return None
        import asyncio  # noqa: PLC0415

        def _run():
            return [list(map(float, v)) for v in self._model.encode(
                texts, normalize_embeddings=True, batch_size=self.batch_size,
                show_progress_bar=False)]

        try:
            return await asyncio.to_thread(_run)
        except Exception as e:  # noqa: BLE001
            logger.warning("本地 embed 失败: %s", e)
            return None


def create_embedding_from_env() -> EmbeddingProvider:
    """按环境变量构建向量器。**默认不加载任何模型**。

    EMBEDDING_OFF=1（或什么都不配）→ NullEmbeddingProvider：规则降级, 不静默调用云端。
    需要向量时显式二选一:
    1) EMBEDDING_BASE_URL → openjiuwen 的 OpenAI 兼容 /embeddings 端点(推荐, 不占内存);
    2) EMBEDDING_LOCAL_DIR → 进程内加载 sentence_transformers(要自己装, 模型几百 MB~几 GB)。

    以前这里会**自动探测项目下的 ./model 并直接加载**——交付时那等于"连着一个本地大模型",
    换台机器就装不上、装上了也吃内存。现在改成必须显式配置, 默认纯规则。
    """
    if (os.getenv("EMBEDDING_OFF") or "").strip().lower() in ("1", "true", "yes"):
        return NullEmbeddingProvider()

    base = (os.getenv("EMBEDDING_BASE_URL") or "").strip()
    if base:
        model = (os.getenv("EMBEDDING_MODEL_NAME") or "Qwen/Qwen3-Embedding-8B").strip()
        key = (os.getenv("EMBEDDING_API_KEY") or "EMPTY").strip()
        return OpenJiwenEmbeddingProvider(base_url=base, model=model, api_key=key)

    local_dir = (os.getenv("EMBEDDING_LOCAL_DIR") or "").strip()
    if local_dir:
        try:
            import sentence_transformers  # noqa: PLC0415, F401
            return LocalSentenceTransformersProvider(local_dir)
        except ImportError:
            logger.info("没装 sentence_transformers, 跳过进程内本地加载(%s); "
                        "核心链路不需要向量, 不受影响", local_dir)
    return NullEmbeddingProvider()
