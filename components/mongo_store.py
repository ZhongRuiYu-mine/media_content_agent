# coding: utf-8
"""components/mongo_store.py — MongoDB 题目库/意图库薄封装(design D6 / T2.0 / T2.1 / T2.3)。

- 连接: env `MONGODB_URI`(默认 mongodb://127.0.0.1:27017/media_content);
- questions: text_hash 唯一索引(规范化去重) + intent_id/status + student 轮询索引;
- intents: name 唯一索引;
- 可用性自检: mongo_available() 带短超时 ping + 负缓存; Mongo 未启动时所有操作
  安全降级(返回 None/False/[]), 调用方回退到直接生成路径, 不静默失败;
- 铁律: 本模块零 LLM 调用, 纯本地存储与规则。
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_client = None
_ok: Optional[bool] = None
_ok_ts = 0.0
_AVAIL_TTL = 30.0  # 可用性缓存秒数(负缓存防每次 2s 超时)


def _uri() -> str:
    return os.getenv("MONGODB_URI", "mongodb://127.0.0.1:27017/media_content")


def _get_client():
    global _client
    if _client is None:
        from pymongo import MongoClient  # noqa: PLC0415 - 惰性导入, 未装驱动不炸
        _client = MongoClient(_uri(), serverSelectionTimeoutMS=2000)
    return _client


def mongo_available() -> bool:
    global _ok, _ok_ts
    now = time.monotonic()
    if _ok is not None and now - _ok_ts < _AVAIL_TTL:
        return _ok
    try:
        _get_client().admin.command("ping")
        _ok, _ok_ts = True, now
        return True
    except Exception as e:  # noqa: BLE001
        _ok, _ok_ts = False, now
        logger.warning("MongoDB 不可用(%s): %s", _uri(), e)
        return False


def normalize_question(text: str) -> str:
    """题目规范化(去重用): 去空白、去尾部标点。"""
    return "".join((text or "").split()).strip().rstrip("。！？!?；;")


def text_hash(text: str) -> str:
    return hashlib.sha1(normalize_question(text).encode("utf-8")).hexdigest()


def init_indexes() -> bool:
    """建索引(幂等; 启动自检用)。"""
    if not mongo_available():
        return False
    try:
        db = _get_client().get_default_database()
        db.questions.create_index("text_hash", unique=True)
        db.questions.create_index([("intent_id", 1), ("status", 1)])
        db.questions.create_index([("student_id", 1), ("asked_count", 1), ("created_ts", 1)])
        db.intents.create_index("name", unique=True)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("MongoDB 索引初始化失败: %s", e)
        return False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class QuestionStore:
    """题目库(T2.1): 入库去重 + 按学生/意图轮询抽取。"""

    COLLECTION = "questions"

    def _coll(self):
        return _get_client().get_default_database()[self.COLLECTION]

    def add(self, text: str, student_id: str = "default", intent_id: str = "",
            source_type: str = "", source_event_ids: Optional[List[str]] = None,
            session_id: str = "", status: str = "active") -> bool:
        """入库(规范化去重)。返回 True=新入库, False=已存在或不可用。"""
        if not text or not text.strip() or not mongo_available():
            return False
        h = text_hash(text)
        doc = {
            "text": text.strip(),
            "text_hash": h,
            "intent_id": intent_id or "",
            "source_type": source_type,
            "source_event_ids": list(source_event_ids or []),
            "student_id": student_id,
            "session_id": session_id,
            "status": status,
            "asked_count": 0,
            "last_asked_ts": None,
            "created_ts": _now(),
        }
        try:
            res = self._coll().update_one(
                {"text_hash": h}, {"$setOnInsert": doc}, upsert=True)
            return res.upserted_id is not None
        except Exception as e:  # noqa: BLE001 - 含 DuplicateKeyError
            logger.warning("题目入库失败: %s", e)
            return False

    def add_many(self, texts: List[str], **kw) -> int:
        """批量入库, 返回新入库条数。"""
        return sum(1 for t in texts if self.add(t, **kw))

    def pop_next(self, student_id: str = "default", intent_id: Optional[str] = None,
                 n: int = 1) -> List[str]:
        """取该学生(可限定意图)active 题中 asked_count 最小者; 取到即 asked_count+1。"""
        if not mongo_available():
            return []
        try:
            q: Dict = {"status": "active", "student_id": student_id}
            if intent_id:
                q["intent_id"] = intent_id
            docs = list(self._coll().find(q)
                        .sort([("asked_count", 1), ("created_ts", 1)])
                        .limit(int(n)))
            out = []
            for d in docs:
                self._coll().update_one(
                    {"_id": d["_id"]},
                    {"$inc": {"asked_count": 1}, "$set": {"last_asked_ts": _now()}})
                out.append(d.get("text", ""))
            return out
        except Exception as e:  # noqa: BLE001
            logger.warning("题目抽取失败: %s", e)
            return []

    def records(self, student_id: Optional[str] = None,
                intent_id: Optional[str] = None) -> List[dict]:
        """题库记录(查看用): student_id=None 表示全部学生。"""
        if not mongo_available():
            return []
        q: Dict = {}
        if student_id:
            q["student_id"] = student_id
        if intent_id:
            q["intent_id"] = intent_id
        try:
            out = []
            for d in self._coll().find(q).sort("created_ts", 1):
                out.append({
                    "text": d.get("text", ""),
                    "student_id": d.get("student_id", ""),
                    "intent_id": d.get("intent_id", ""),
                    "source_type": d.get("source_type", ""),
                    "status": d.get("status", "active"),
                    "asked_count": d.get("asked_count", 0),
                    "last_asked_ts": d.get("last_asked_ts"),
                    "created_ts": d.get("created_ts", ""),
                })
            return out
        except Exception as e:  # noqa: BLE001
            logger.warning("题库记录读取失败: %s", e)
            return []

    def count(self, student_id: str = "default", intent_id: Optional[str] = None) -> int:
        if not mongo_available():
            return 0
        try:
            q: Dict = {"student_id": student_id}
            if intent_id:
                q["intent_id"] = intent_id
            return self._coll().count_documents(q)
        except Exception:  # noqa: BLE001
            return 0

    def all_texts(self, student_id: str = "default",
                  intent_id: Optional[str] = None) -> List[str]:
        """该学生(可限定意图)全部 active 题文本(供覆盖检查做向量匹配)。"""
        if not mongo_available():
            return []
        q: Dict = {"status": "active", "student_id": student_id}
        if intent_id:
            q["intent_id"] = intent_id
        try:
            return [d.get("text", "") for d in self._coll().find(q, {"text": 1})]
        except Exception:  # noqa: BLE001
            return []

    def pop_random(self, student_id: str = "default",
                   intent_id: Optional[str] = None, n: int = 1,
                   only_generic: bool = False,
                   exclude: Optional[List[str]] = None) -> List[str]:
        """随机抽题(无薄弱无上文时用); 取到即 asked_count+1。

        only_generic=True: 只抽通用题(排除薄弱点关联题, 防同一题经意图路径重复抽回)。
        exclude: 排除这些题目文本(去重守卫: 本轮已问/紧邻上一题)。候选全部被排除时返回空,
                 由调用方回退到 LLM/模板, 而不是硬重复同一道。
        """
        if not mongo_available():
            return []
        try:
            q: Dict = {"status": "active", "student_id": student_id}
            if intent_id:
                q["intent_id"] = intent_id
            if only_generic:
                q["source_event_ids"] = {"$size": 0}
            ex = [text_hash(str(t)) for t in (exclude or []) if str(t or "").strip()]
            if ex:
                q["text_hash"] = {"$nin": ex}
            docs = list(self._coll().aggregate([
                {"$match": q}, {"$sample": {"size": int(n)}}]))
            out = []
            for d in docs:
                self._coll().update_one(
                    {"_id": d["_id"]},
                    {"$inc": {"asked_count": 1}, "$set": {"last_asked_ts": _now()}})
                out.append(d.get("text", ""))
            return out
        except Exception as e:  # noqa: BLE001
            logger.warning("题目随机抽取失败: %s", e)
            return []

    def min_asked_by_event(self, event_id: str) -> Optional[int]:
        """该薄弱点关联题中已被问过的最小次数(无关联题 → None)。"""
        if not event_id or not mongo_available():
            return None
        try:
            docs = list(self._coll().find(
                {"source_event_ids": event_id, "status": "active"},
                {"asked_count": 1}))
            if not docs:
                return None
            return min(int(d.get("asked_count", 0)) for d in docs)
        except Exception:  # noqa: BLE001
            return None

    def pop_specific(self, student_id: str, text: str) -> bool:
        """按文本抽取指定题(覆盖向量命中时计一次提问); 返回是否命中。"""
        if not text or not mongo_available():
            return False
        try:
            res = self._coll().update_one(
                {"text_hash": text_hash(text), "status": "active"},
                {"$inc": {"asked_count": 1}, "$set": {"last_asked_ts": _now()}})
            return res.modified_count > 0
        except Exception as e:  # noqa: BLE001
            logger.warning("指定题抽取失败: %s", e)
            return False

    def count_by_event(self, event_id: str) -> int:
        """与该薄弱点事件关联的题目数(source_event_ids 含 event_id)。"""
        if not event_id or not mongo_available():
            return 0
        try:
            return self._coll().count_documents({"source_event_ids": event_id})
        except Exception:  # noqa: BLE001
            return 0

    def pop_next_by_event(self, student_id: str, event_id: str,
                          exclude: Optional[List[str]] = None) -> str:
        """按薄弱点事件关联题轮询抽取(asked_count 最小优先), 取到即计数。

        exclude: 排除这些题目文本(去重守卫: 本轮已问/紧邻上一题)。候选全部被排除时
                 返回空, 由调用方回退到其它素材, 而不是硬重复同一道。
        """
        if not event_id or not mongo_available():
            return ""
        try:
            q: Dict = {"status": "active", "student_id": student_id,
                       "source_event_ids": event_id}
            ex = [text_hash(str(t)) for t in (exclude or []) if str(t or "").strip()]
            if ex:
                q["text_hash"] = {"$nin": ex}
            docs = list(self._coll().find(q)
                        .sort([("asked_count", 1), ("created_ts", 1)]).limit(1))
            if not docs:
                return ""
            d = docs[0]
            self._coll().update_one(
                {"_id": d["_id"]},
                {"$inc": {"asked_count": 1}, "$set": {"last_asked_ts": _now()}})
            return d.get("text", "")
        except Exception as e:  # noqa: BLE001
            logger.warning("薄弱点关联题抽取失败: %s", e)
            return ""


# 事件类型 → 意图 id(与 design/03 event schema 对齐)
EVENT_TYPE_INTENT = {
    "project": "project",
    "competition": "competition",
    "education": "education",
    "experience": "experience",
    "skill": "skill",
    "achievement": "achievement",
}


class IntentStore:
    """意图库 v1(T2.3 完整): 规则关键词 + 本地向量匹配(样本质心), 样本沉淀进 Mongo。

    向量匹配复用全局 embedding Provider(与阶段1 审批分类同一个模型), 仅在
    关键词未命中时触发; 样本质心按 (name, 样本数) 缓存, 新增样本自然失效。
    """

    COLLECTION = "intents"

    _centroid_cache: Dict = {}
    _CENTROID_CACHE_MAX = 128

    KEYWORD_INTENTS = [
        ("project", ["项目", "系统", "平台", "网站", "应用", "架构", "开发", "实现",
                     "技术栈", "优化", "部署", "算法"]),
        ("competition", ["竞赛", "比赛", "大赛", "建模", "获奖", "金奖", "银奖",
                         "一等奖", "二等奖", "三等奖"]),
        ("education", ["大学", "学院", "学校", "专业", "gpa", "绩点", "课程", "本科", "硕士"]),
        ("experience", ["实习", "工作", "任职", "岗位", "负责", "团队", "公司"]),
        ("skill", ["技能", "掌握", "熟悉", "会用", "使用"]),
        ("achievement", ["论文", "专利", "成果", "发表", "录用"]),
    ]

    def _coll(self):
        return _get_client().get_default_database()[self.COLLECTION]

    @classmethod
    def match_intent(cls, text: str) -> Optional[str]:
        """本地规则匹配(无 LLM); 命中关键词数最多的意图胜出, 全未命中返回 None。"""
        low = (text or "").lower()
        best, best_n = None, 0
        for name, kws in cls.KEYWORD_INTENTS:
            n = sum(1 for k in kws if k.lower() in low)
            if n > best_n:
                best, best_n = name, n
        return best

    @classmethod
    def intent_of_event(cls, event: Optional[dict]) -> Optional[str]:
        if not event:
            return None
        return EVENT_TYPE_INTENT.get(event.get("type")) or cls.match_intent(
            f"{event.get('subject', '')} {event.get('content', '')}")

    @staticmethod
    def _l2(vec: list) -> list:
        import math
        n = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / n for x in vec]

    async def match_intent_vectors(self, text: str, embedder,
                                   threshold: float = 0.55) -> Optional[str]:
        """T2.3 后半: 向量匹配(仅返回意图名, 兼容旧调用)。"""
        name, _ = await self._match_vectors_conf(text, embedder, threshold)
        return name

    async def _match_vectors_conf(self, text: str, embedder,
                                  threshold: float = 0.55):
        """向量匹配 + 置信度: 与各意图样本质心的 cosine 最大者 ≥ threshold 即命中。

        Returns (intent, cos) 或 (None, 0.0)。
        """
        if not (text or "").strip():
            return None, 0.0
        if embedder is None or not embedder.available():
            return None, 0.0
        if not mongo_available():
            return None, 0.0
        try:
            docs = list(self._coll().find(
                {"samples.0": {"$exists": True}}, {"name": 1, "samples": 1}))
        except Exception as e:  # noqa: BLE001
            logger.warning("意图样本读取失败: %s", e)
            return None, 0.0
        if not docs:
            return None, 0.0
        qvecs = await embedder.embed_documents([text])
        if not qvecs:
            return None, 0.0
        qv = qvecs[0]
        best, best_c = None, 0.0
        for d in docs:
            samples = d.get("samples") or []
            texts = [f"{(s.get('question') or '')} {(s.get('answer') or '')}".strip()
                     for s in samples]
            texts = [t for t in texts if t]
            if not texts:
                continue
            key = (d["name"], len(texts))
            centroid = self._centroid_cache.get(key)
            if centroid is None:
                got = await embedder.embed_documents(texts)
                if not got:
                    continue
                dim = len(got[0])
                centroid = self._l2([sum(v[i] for v in got) / len(got)
                                     for i in range(dim)])
                if len(self._centroid_cache) >= self._CENTROID_CACHE_MAX:
                    self._centroid_cache.clear()
                self._centroid_cache[key] = centroid
            c = embedder.cosine(qv, centroid)
            if c > best_c:
                best, best_c = d["name"], c
        return (best, best_c) if best_c >= threshold else (None, best_c)

    def _samples_by_intent(self) -> Dict[str, List[str]]:
        """拉取全部意图样本文本(question+answer), 供学习器(TF-IDF+LR)训练。"""
        if not mongo_available():
            return {}
        try:
            out: Dict[str, List[str]] = {}
            for d in self._coll().find({}, {"name": 1, "samples": 1}):
                texts = []
                for s in d.get("samples") or []:
                    t = f"{(s.get('question') or '')} {(s.get('answer') or '')}".strip()
                    if t:
                        texts.append(t[:400])
                if texts:
                    out[d["name"]] = texts
            return out
        except Exception as e:  # noqa: BLE001
            logger.warning("意图样本读取失败: %s", e)
            return {}

    async def detect_intent(self, text: str, embedder):
        """完整意图判定(调度器大脑, 用户定稿 3/4 点):
        规则关键词 → 学习器(TF-IDF+逻辑回归, 样本累积后启用) → 嵌入质心。

        Returns (intent, confidence): 关键词恒 1.0; 学习器为最大概率; 质心为 cosine。
        ≥0.65 视为强识别直接信任; 0.55–0.65 弱识别(交用户授权打标/手动)。
        """
        kw = self.match_intent(text)
        if kw:
            return kw, 1.0
        from components import intent_learner
        learner = intent_learner.get_learner()
        if learner.refresh(self._samples_by_intent()):
            hit = learner.predict(text)
            if hit and hit[1] >= 0.45:
                return hit
        name, cos = await self._match_vectors_conf(text, embedder)
        return (name, cos) if name else (None, 0.0)

    def record_sample(self, name: str, question: str, answer: str,
                      matched_locally: bool = True, source: str = "labeled") -> bool:
        """把(问题,回答)样本沉淀为意图语料(供后续匹配/LLM 兜底复用)。

        source: labeled(授权打标) | self_supervised(事件 type→意图, 零 LLM, design/07 §5.1)。
        """
        if not name or not mongo_available():
            return False
        try:
            sample = {"question": (question or "")[:400],
                      "answer": (answer or "")[:400],
                      "source": source,
                      "matched_locally": bool(matched_locally),
                      "ts": _now()}
            self._coll().update_one(
                {"name": name},
                {"$setOnInsert": {"name": name, "keywords": [],
                                  "need_authorization": False},
                 "$push": {"samples": {"$each": [sample], "$slice": -200}}},
                upsert=True)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("意图样本沉淀失败: %s", e)
            return False
