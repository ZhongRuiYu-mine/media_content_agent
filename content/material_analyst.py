# coding: utf-8
"""content/material_analyst.py — 素材录入(design/11 §5.5 第 1 步: material_intake)。

把散料变成**素材图**: 切句 → 逐句裁决(内容域裁决矩阵) → 权威度 + 类型 + 时间,
一次 commit 落盘, 随 seq 可回滚。与素材图的分工:

- 素材图服务于在线素材的增删(可回滚);
- 本模块服务于内容域的"素材录入": 多了 `authority` / `verified` / `sensitive`
  三个内容域字段(design/11 §4.1), 类型走 `guess_material_type`(六类素材),
  裁决走启用 R7 场地比对的裁决矩阵(design/11 §3.2)。

零 LLM: 切句、类型、权威度、逐句裁决全部确定性。
"""
from __future__ import annotations

from typing import Dict, List, Optional

from graphstore.types import (
    MATERIAL_TYPES, Period, extract_metrics, extract_techs, norm_text,
    guess_material_type,
)

from content.material_ops import infer_authority

_SPLIT_RE = None


def _split_sentences(text: str) -> List[str]:
    """按句末标点/换行切句(与前端 doc-board.js 同口径, 保持切句框一致)。"""
    from content.intake_verdict import split_sentences
    return split_sentences(text)


def _guess_subject(text: str, mtype: str, techs: List[str]) -> str:
    """素材主体: 优先事件/活动名(内容域规则), 回退到通用启发式抽取。"""
    import re
    t = text or ""
    for pat in (
        r"([\u4e00-\u9fffA-Za-z0-9·]{2,24}?(?:比赛|竞赛|大赛|活动|讲座|沙龙|分享会|培训|晚会|路演|纳新|招新))",
        r"([\u4e00-\u9fffA-Za-z0-9·]{2,24}?(?:通知|公告))",
    ):
        m = re.search(pat, t)
        if m:
            return m.group(1)
    from content.intake_verdict import guess_subject
    return guess_subject(text, mtype, techs)


def build_material_record(rec: dict) -> dict:
    """把一条素材输入规范化成 graphstore 事件记录(design/11 §4.1)。

    输入: {content, type?, subject?, source?, period?, verified?, sensitive?, keywords?}
    产出: 完整 record(含 authority), 交给 `FileGraphStore.commit("ADD_EVENT", ...)`。
    """
    content = (rec.get("content") or "").strip()
    source = (rec.get("source") or "member").strip()
    techs = list(rec.get("techs") or []) or extract_techs(content)
    period = rec.get("period")
    period_obj = Period.parse(content) if not period else None
    mtype = rec.get("type") or guess_material_type(content, rec.get("subject", ""),
                                                  techs, period)
    if mtype not in MATERIAL_TYPES:
        mtype = guess_material_type(content, rec.get("subject", ""), techs, period)
    metrics = rec.get("metrics") or [m.to_dict() for m in extract_metrics(content)]
    keywords = list(rec.get("keywords") or [])
    core = norm_text(content)
    keywords += [core[i:i + 2] for i in range(max(0, len(core) - 1))][:6]
    keywords = list(dict.fromkeys([k for k in keywords if k]))
    return {
        "type": mtype,
        "subject": rec.get("subject") or _guess_subject(content, mtype, techs) or "未命名素材",
        "content": content,
        "source": source,
        "role": rec.get("role"), "org": rec.get("org"),
        "period": period if isinstance(period, dict) else (
            period_obj.to_dict() if period_obj else None),
        "keywords": keywords, "techs": techs,
        "metrics": metrics, "claims": rec.get("claims") or [],
        "authority": rec.get("authority") if rec.get("authority") is not None
        else infer_authority(source),
        "verified": bool(rec.get("verified", False)),
        "sensitive": bool(rec.get("sensitive", False)),
    }


async def analyze_material_text(text: str, events: Optional[Dict[str, dict]] = None,
                                embedder=None) -> List[dict]:
    """素材文本 → 逐句裁决(内容域: 启用 R7 场地比对)。

    与 `/api/memory/analyze` 的差别只在裁决矩阵: 这里 content_rules=True,
    "时间+地点"两个字段都参与比对(design/11 §3.2)。
    """
    from components.classifier import classify
    from content.intake_verdict import simplify_verdict, split_sentences

    evs = events or {}
    sentences = split_sentences(text)
    out: List[dict] = []
    for sid, sentence in enumerate(sentences):
        verdict = await classify(sentence, evs, embedder=embedder, content_rules=True)
        verdict = simplify_verdict(verdict, evs)
        out.append({"sid": sid, "text": sentence, "verdict": verdict,
                    "material_type": guess_material_type(sentence, "", [], None),
                    "authority": infer_authority("member")})
    return out


def ingest_materials(store, records: List[dict], session_id: str = "",
                     default_source: str = "member") -> List[dict]:
    """批量录入素材(素材图落地: 类型/权威度/时间/cites 子图就位)。

    store: 已 open 的 FileGraphStore(或 MemoryService.store)。
    返回 [{eid, seq, subject, type, source, authority, verified, sensitive}]
    """
    out: List[dict] = []
    for rec in records or []:
        r = dict(rec)
        r.setdefault("source", default_source)
        material = build_material_record(r)
        if not material["content"]:
            continue
        res = store.commit("ADD_EVENT", {"record": material},
                           session_id=session_id or "material_intake")
        out.append({
            "eid": f"evt_{res['seq']:06d}", "seq": res["seq"],
            "subject": material["subject"], "type": material["type"],
            "source": material["source"], "authority": material["authority"],
            "verified": material["verified"], "sensitive": material["sensitive"],
            "period": material["period"],
        })
    return out


def materials_of(state: dict) -> List[dict]:
    """从图 state 取素材列表(按 created_seq 升序 = 录入顺序 = 时间线)。"""
    events = (state or {}).get("events", {}) or {}
    rows = [dict(ev) for ev in events.values()]
    rows.sort(key=lambda x: (x.get("created_seq") or 0))
    return rows


def material_badge(ev: dict) -> dict:
    """素材的前端徽章数据(权威度 5 档 + 类型 + 隐私/已确认标记)。"""
    from graphstore.types import authority_band
    a = float(ev.get("authority") or 0.0)
    return {
        "eid": ev.get("eid"),
        "authority": a,
        "band": authority_band(a),
        "source": ev.get("source"),
        "type": ev.get("type"),
        "verified": bool(ev.get("verified")),
        "sensitive": bool(ev.get("sensitive")),
        "period": (ev.get("period") or {}).get("raw") if isinstance(ev.get("period"), dict) else None,
    }


__all__ = ["build_material_record", "analyze_material_text", "ingest_materials",
           "materials_of", "material_badge"]
