# coding: utf-8
"""components/intent_learner.py — 意图持续学习模型(用户定稿 4/5 点, 稳定大于创新)。

成熟路线:
  - 特征: 字符 n-gram(1-3) TF-IDF(sklearn);
  - 模型: 多分类逻辑回归(lbfgs, class_weight=balanced);
  - 训练数据: Mongo intents 集合的 samples —— 每个样本即
    "上一轮问题 + 上一轮回答"(其打标意图 = 集合名), 持续沉淀持续重训;
  - 训练时机: 懒刷新(样本总数变化才重训, 样本量小, 毫秒级);
  - 兜底: sklearn 缺失/样本不足时不可用, 由规则关键词与嵌入质心兜底(不报错)。
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_MIN_SAMPLES_PER_CLASS = 3   # 冷启动保护: 单意图样本不足时不启用模型
_MIN_CLASSES = 2             # 至少两个意图同时达标才训练(单类逻辑回归无意义)


class IntentLearner:
    def __init__(self):
        self._vec = None
        self._clf = None
        self._trained_n = -1

    @property
    def trained(self) -> bool:
        return self._clf is not None

    def refresh(self, samples: Dict[str, List[str]]) -> bool:
        """样本总数变化 → 重训; 返回当前是否可用。"""
        total = sum(len(v) for v in samples.values())
        if total == self._trained_n:
            return self.trained
        self._trained_n = total
        self._clf = None
        usable = {k: v for k, v in samples.items()
                  if len(v) >= _MIN_SAMPLES_PER_CLASS}
        if len(usable) < _MIN_CLASSES:
            return False
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer
            from sklearn.linear_model import LogisticRegression
            docs: List[str] = []
            labels: List[str] = []
            for name, texts in usable.items():
                for t in texts:
                    docs.append(t)
                    labels.append(name)
            vec = TfidfVectorizer(analyzer="char", ngram_range=(1, 3), min_df=1)
            X = vec.fit_transform(docs)
            clf = LogisticRegression(max_iter=1000, class_weight="balanced")
            clf.fit(X, labels)
            self._vec, self._clf = vec, clf
            logger.info("意图学习器重训完成: samples=%d labels=%s",
                        total, sorted(usable.keys()))
        except Exception as e:  # noqa: BLE001 - 降级质心, 不阻塞
            logger.warning("意图学习器训练失败(降级质心): %s", e)
            self._clf = None
        return self.trained

    def predict(self, text: str) -> Optional[Tuple[str, float]]:
        """返回 (意图, 置信度=最大概率); 不可用返回 None。"""
        if self._clf is None or not (text or "").strip():
            return None
        try:
            X = self._vec.transform([text])
            probs = self._clf.predict_proba(X)[0]
            classes = self._clf.classes_
            i = int(probs.argmax())
            return classes[i], float(probs[i])
        except Exception as e:  # noqa: BLE001
            logger.warning("意图预测失败: %s", e)
            return None


_LEARNER = IntentLearner()


def get_learner() -> IntentLearner:
    """进程级单例(样本总量小, 重训毫秒级, 无需跨进程持久化)。"""
    return _LEARNER
