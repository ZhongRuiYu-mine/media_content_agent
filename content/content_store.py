# coding: utf-8
"""content/content_store.py — 选题库(design/11 §5.2 / §9 T2.3)。

- `TopicStore`: 选题入库 + **轮询** `pop_next`(保持 `asked_count` 计数与 `exclude`
  去重守卫, 与原 `QuestionStore` 同口径, design/11 §5.3 候选源 5);
- 后端优先 MongoDB(与原项目一致), **不可用自动降级为本地文件**(design/11 §12 R5:
  演示现场 Mongo 起不来也不能卡住, 所以选题库必须有 File 兜底分支);
- 去重: 同一 subject + kind 视为同一条(重复扫描不重复入库), `seen_count` 累加。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILE_BACKEND = "file"
MONGO_BACKEND = "mongo"


def _safe(sid: str) -> str:
    return "".join(c for c in (sid or "") if c.isalnum() or c in "-_") or "default"


class TopicStore:
    """选题库: Mongo 优先, File 兜底(接口一致, 调用方无感)。"""

    def __init__(self, student_id: str = "default", backend: Optional[str] = None):
        self.student_id = _safe(student_id)
        self.backend = backend or os.getenv("TOPIC_STORE_BACKEND", "").strip()
        self._mongo_ok: Optional[bool] = None
        self._path = os.path.join(
            os.getenv("TOPIC_BANK_DIR", os.path.join(_BASE, "data", "topic_bank")),
            f"{self.student_id}.json")
        if not self.backend:
            self.backend = MONGO_BACKEND if self._try_mongo() else FILE_BACKEND
        elif self.backend == MONGO_BACKEND and not self._try_mongo():
            self.backend = FILE_BACKEND

    # ---------- Mongo(可选) ----------
    def _try_mongo(self) -> bool:
        if self._mongo_ok is not None:
            return self._mongo_ok
        try:
            from components.mongo_store import init_indexes, mongo_available
            self._mongo_ok = bool(mongo_available())
            if self._mongo_ok:
                self._ensure_index()
        except Exception as e:  # noqa: BLE001
            logger.info("选题库 Mongo 不可用, 走 File 兜底: %s", e)
            self._mongo_ok = False
        return bool(self._mongo_ok)

    def _col(self):
        """选题集合(复用原项目的连接与降级口径)。"""
        from components.mongo_store import _get_client
        return _get_client().get_default_database()["topics"]

    def _ensure_index(self) -> None:
        try:
            self._col().create_index([("student_id", 1), ("id", 1)], unique=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("选题库索引创建失败: %s", e)

    # ---------- 文件后端 ----------
    def _load_file(self) -> List[dict]:
        if not os.path.exists(self._path):
            return []
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return list(data.get("topics") or []) if isinstance(data, dict) else list(data)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("选题库文件读取失败: %s", e)
            return []

    def _save_file(self, rows: List[dict]) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"topics": rows}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)

    # ---------- 对外 API ----------
    def add(self, topic: dict) -> dict:
        """入库一条选题(按 subject+kind 去重; 返回 {added, seen_count, id?})。"""
        subject = (topic.get("subject") or "").strip()
        kind = topic.get("kind") or ""
        if not subject:
            return {"added": False, "error": "选题主题为空"}
        rows = self.records()
        for r in rows:
            if r.get("subject") == subject and r.get("kind") == kind:
                r["seen_count"] = int(r.get("seen_count", 1)) + 1
                r["score"] = max(float(r.get("score") or 0), float(topic.get("score") or 0))
                r["reason"] = topic.get("reason") or r.get("reason")
                self._write_all(rows)
                return {"added": False, "seen_count": r["seen_count"], "id": r.get("id")}
        rec = {
            "id": f"topic_{len(rows) + 1:04d}",
            "student_id": self.student_id,
            "kind": kind,
            "label": topic.get("label") or "",
            "subject": subject,
            "reason": topic.get("reason") or "",
            "score": float(topic.get("score") or 0.0),
            "gap_count": int(topic.get("gap_count") or 0),
            "authority_mean": float(topic.get("authority_mean") or 0.0),
            "eids": list(topic.get("eids") or []),
            "seen_count": 1,
            "asked_count": 0,
            "source": topic.get("source") or "scan",
        }
        rows.append(rec)
        self._write_all(rows)
        return {"added": True, "seen_count": 1, "id": rec["id"]}

    def add_many(self, topics: List[dict]) -> dict:
        added = 0
        for t in topics or []:
            if self.add(t).get("added"):
                added += 1
        return {"added": added, "total": len(self.records())}

    def records(self, student_id: Optional[str] = None) -> List[dict]:
        if self.backend == MONGO_BACKEND:
            try:
                rows = list(self._col().find({"student_id": self.student_id},
                                             {"_id": 0}))
                rows.sort(key=lambda r: -float(r.get("score") or 0))
                return rows
            except Exception as e:  # noqa: BLE001
                logger.warning("选题库 Mongo 读取失败, 回退文件: %s", e)
                self.backend = FILE_BACKEND
        rows = self._load_file()
        rows.sort(key=lambda r: -float(r.get("score") or 0))
        return rows

    def _write_all(self, rows: List[dict]) -> None:
        if self.backend == MONGO_BACKEND:
            try:
                col = self._col()
                for r in rows:
                    r.setdefault("student_id", self.student_id)
                    col.replace_one({"student_id": self.student_id, "id": r["id"]},
                                    r, upsert=True)
                return
            except Exception as e:  # noqa: BLE001
                logger.warning("选题库 Mongo 写入失败, 回退文件: %s", e)
                self.backend = FILE_BACKEND
        self._save_filesafe(rows)

    def _save_filesafe(self, rows: List[dict]) -> None:
        merged = {r.get("id"): r for r in self._load_file()}
        for r in rows:
            merged[r.get("id")] = r
        self._save_file(list(merged.values()))

    def pop_next(self, exclude: Optional[List[str]] = None) -> Optional[dict]:
        """轮询取下一条选题(design/11 §5.3 候选源 5: 保持 asked_count 轮询与 exclude 去重)。

        口径与原 QuestionStore.pop_next 一致: 取 asked_count 最小者 → 分数次之;
        排除 exclude 里的 id; 取中后 asked_count+1(写回)。
        """
        ex = set(exclude or [])
        rows = [r for r in self.records() if r.get("id") not in ex]
        if not rows:
            return None
        rows.sort(key=lambda r: (int(r.get("asked_count", 0)), -float(r.get("score") or 0)))
        picked = rows[0]
        picked["asked_count"] = int(picked.get("asked_count", 0)) + 1
        self._write_all([picked])
        return picked

    def count(self) -> int:
        return len(self.records())

    # ---------- 用户裁决记录(人在环里: 矛盾由用户选口径) ----------
    def _res_path(self) -> str:
        return os.path.join(
            os.getenv("RESOLUTION_DIR", os.path.join(_BASE, "data", "resolutions")),
            f"{self.student_id}.json")

    def resolutions(self) -> List[dict]:
        """已由用户裁决的矛盾(按 claim_key 去重)。"""
        import json as _json
        p = self._res_path()
        if not os.path.exists(p):
            return []
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = _json.load(f)
            return list(data.get("resolutions") or []) if isinstance(data, dict) else list(data)
        except (OSError, ValueError) as e:  # noqa: BLE001
            logger.warning("裁决记录读取失败: %s", e)
            return []

    def add_resolution(self, claim_key: str, chosen_eid: str,
                       seg_id: str = "", note: str = "") -> dict:
        """记一条用户裁决。**裁决必须在重跑核验之后依然有效**, 所以单独落盘。"""
        import json as _json
        rows = [r for r in self.resolutions() if r.get("claim_key") != claim_key]
        rec = {"claim_key": claim_key, "chosen_eid": chosen_eid, "seg_id": seg_id,
               "note": note, "ts": _now()}
        rows.append(rec)
        p = self._res_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump({"resolutions": rows}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
        return rec

    def clear(self) -> None:
        if self.backend == MONGO_BACKEND:
            try:
                self._col().delete_many({"student_id": self.student_id})
                return
            except Exception:  # noqa: BLE001
                self.backend = FILE_BACKEND
        self._save_file([])


def _now() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


__all__ = ["TopicStore", "FILE_BACKEND", "MONGO_BACKEND"]
