# coding: utf-8
"""content/content_versions.py — 稿件版本(design/11 §5.4: 两轨都存版本, 可对比可回退)。

版本号 = 素材图的 seq —— 与 `components/resume_versions.py` 同一口径的原项目约定
(design/11 §3.1: "版本号仍 = 图 seq"), 这样"回滚素材图到某 seq"与"稿件回到那一版"
是同一件事, 不需要第二套时间轴。

每条记录:
  {seq, topic, track, text, segments, block, llm_used, ts}
  track: base(轨一, 零 LLM 事实基线) | polished(轨二, 授权润色)
"""
from __future__ import annotations

import json
import logging
import os
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _safe(sid: str) -> str:
    return "".join(c for c in (sid or "") if c.isalnum() or c in "-_") or "default"


def versions_path(student_id: str) -> str:
    d = os.getenv("CONTENT_VERSIONS_DIR", os.path.join(_BASE, "data", "content_versions"))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{_safe(student_id)}.jsonl")


def save_version(student_id: str, seq: int, draft: dict, track: str = "base",
                 llm_used: bool = False, text: Optional[str] = None) -> dict:
    """存一版稿件(追加一行; 同一 seq 同一轨重复保存则覆盖旧行)。"""
    rec = {
        "seq": int(seq),
        "topic": draft.get("topic") or "",
        "track": track,
        "text": text if text is not None else draft_to_text(draft),
        "segments": draft.get("segments") or [],
        "block": bool(draft.get("block")),
        "llm_used": bool(llm_used),
        "ts": draft.get("ts") or "",
    }
    path = versions_path(student_id)
    rows = [r for r in _read(path) if not (int(r.get("seq", -1)) == rec["seq"]
                                           and r.get("track") == track)]
    rows.append(rec)
    rows.sort(key=lambda r: (int(r.get("seq", 0)), 0 if r.get("track") == "base" else 1))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    return {"seq": rec["seq"], "track": track, "saved": True}


def _read(path: str) -> List[dict]:
    if not os.path.exists(path):
        return []
    out: List[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError as e:  # noqa: BLE001
        logger.warning("稿件版本读取失败: %s", e)
    return out


def get_version(student_id: str, seq: int, track: str = "base") -> Optional[dict]:
    for r in _read(versions_path(student_id)):
        if int(r.get("seq", -1)) == int(seq) and r.get("track") == track:
            return r
    return None


def get_any_version(student_id: str, seq: int, track: str = "base") -> Optional[dict]:
    """取某 seq 的稿件版本(默认轨一事实基线; 没有轨一则取轨二)。

    **必须跳过平台渲染变体**(`base:wechat` 之类): 它们是同一 seq 上的平台段结构,
    不是稿件本身。否则 `/api/content/versions?seq=N`、`draft_at()` 与
    `current_draft()` 在"先核对后适配/先适配后回调"的顺序里会返回某个平台版,
    前端拿到的就是平台段名而不是稿件六段(实测: 重出裁决后稿件被覆盖成平台版)。
    """
    rows = [r for r in _read(versions_path(student_id))
            if int(r.get("seq", -1)) == int(seq) and not _is_track_variant(r)]
    if not rows:
        return None
    want = str(track or "base")
    rows.sort(key=lambda r: 0 if r.get("track") == want else (
        1 if r.get("track") == "base" else 2))
    return rows[0]


def _is_track_variant(r: dict) -> bool:
    """平台渲染变体(`base:wechat` 之类)不算"稿件版本"。

    `ContentRuntime.platform_versions` 把每个平台的渲染结果按同一 seq 存一行, 段结构与
    稿件版不同(平台段名)。它们**不能**参与"当前稿件/版本下拉"的挑选, 否则多平台适配
    会把稿件版本基线污染成某个平台版(design/11 §5.4: 轨一事实基线才是可回退的基线)。
    """
    return ":" in str((r or {}).get("track") or "")


def versions(student_id: str, limit: int = 50, include_variants: bool = False) -> List[dict]:
    """版本列表(新→旧), 供前端"版本下拉 + 回滚入口"。

    include_variants=False(默认)只列稿件版本(轨一/轨二), 不列平台渲染变体。
    """
    rows = [r for r in _read(versions_path(student_id))
            if include_variants or not _is_track_variant(r)]
    rows.sort(key=lambda r: (-int(r.get("seq", 0)),
                             0 if r.get("track") == "base" else 1))
    return [{"seq": r.get("seq"), "topic": r.get("topic"), "track": r.get("track"),
             "track_label": track_label(r.get("track")),
             "block": bool(r.get("block")), "llm_used": bool(r.get("llm_used")),
             "chars": len("".join((r.get("text") or "").split())),
             "ts": r.get("ts")} for r in rows[:limit]]


def latest(student_id: str, prefer_compose: bool = True) -> Optional[dict]:
    """最新稿件版本（= 当前稿件）。跳过平台渲染变体。

    compose 轨（用户拖出来的组装稿）优先：刷新页面时用户看到的应该是自己改的那版；
    平台渲染与版本回滚另走 baseline()（轨一事实基线）。
    """
    rows = [r for r in _read(versions_path(student_id)) if not _is_track_variant(r)]
    if not rows:
        return None
    if prefer_compose:
        comp = [r for r in rows if r.get("track") == "compose"]
        if comp:
            comp.sort(key=lambda r: -int(r.get("seq", 0)))
            return comp[0]
    rows.sort(key=lambda r: (-int(r.get("seq", 0)),
                             0 if r.get("track") == "base" else 1))
    return rows[0]


def baseline(student_id: str) -> Optional[dict]:
    """轨一事实基线（系统自动成稿）：平台渲染与联动回滚都认它。"""
    rows = [r for r in _read(versions_path(student_id)) if r.get("track") == "base"]
    if not rows:
        return None
    rows.sort(key=lambda r: -int(r.get("seq", 0)))
    return rows[0]


TRACK_LABELS = {"base": "轨一·事实基线", "polished": "轨二·授权润色",
                "compose": "组装稿·你拖的"}


def track_label(track: str) -> str:
    """轨道标签; 平台变体 `base:wechat` → "平台版·wechat"。"""
    t = str(track or "")
    if t in TRACK_LABELS:
        return TRACK_LABELS[t]
    if t.startswith("base:"):
        return "平台版·" + t.split(":", 1)[1]
    return t


def draft_to_text(draft: dict) -> str:
    """段结构稿 → 纯文本(标题 + 段)。确定性, 零 LLM。"""
    lines = [f"# {draft.get('topic') or '未命名稿件'}"]
    for seg in draft.get("segments") or []:
        label = seg.get("label") or seg.get("kind") or ""
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"\n## {label}\n{text}")
    return "\n".join(lines)


def text_of(rec: Optional[dict]) -> str:
    return (rec or {}).get("text") or ""


__all__ = ["save_version", "get_version", "get_any_version", "versions", "latest",
           "baseline",
           "versions_path", "draft_to_text", "text_of", "TRACK_LABELS", "track_label"]
