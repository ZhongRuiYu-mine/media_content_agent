# coding: utf-8
"""content/fact_verifier.py — 事实核验: 断言抽取 + 支撑判定 + 五类缺口生成。

design/11 §5.1 与 §4.3 的落地, **全确定性、零 LLM**。这是本项目"不替用户造事实"
这条边界的执行机构: 稿件里每个数字/人名/职务/时间/地点都要在素材库找到支撑句,
找不到就是 `no_source`, 阻断发布并进《待核实清单》。

五类缺口(design/11 §4.3):
  no_source 无据  断言在素材库找不到支撑句                  → 阻断
  conflict  矛盾  同指标不同值 / 时间地点与通知不符 / 职务不一致 → 阻断
  sensitive 涉隐私 命中电话/身份证/住址/学号模式             → 阻断
  isolated  孤证  仅出自单一低权威源且无第二来源             → 警告
  stale     过期  period 早于本轮活动窗口                    → 警告

LLM 唯一的兜底是 P22(两份素材冲突时判"哪份为准"), 且必须把两个版本并列交用户裁决
(design/11 §5.1 / §6); 本模块默认不调用它, 冲突一律并列上报。
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Tuple

from graphstore.types import (
    CONFLICT, GAP_TYPES, ISOLATED, LOW_AUTHORITY, NO_SOURCE, Period, SENSITIVE, STALE,
    extract_metrics,
)

from content.material_ops import (
    claim_key, extract_entities, find_support, segment_claims,
)
from content.material_ops import claim_in_text

# 缺口优先级(design/11 §4.3: conflict(0) > no_source(1) > sensitive(2) > isolated(3) > stale(4))
GAP_ORDER = {CONFLICT: 0, NO_SOURCE: 1, SENSITIVE: 2, ISOLATED: 3, STALE: 4}
GAP_PRIORITY = {CONFLICT: 0.90, NO_SOURCE: 0.85, SENSITIVE: 0.80,
                ISOLATED: 0.60, STALE: 0.40}
# 阻断类(不许进入多平台适配)与警告类
BLOCKING_TYPES = (CONFLICT, NO_SOURCE, SENSITIVE)
WARNING_TYPES = (ISOLATED, STALE)

# ---------- 隐私模式(命中即 sensitive; 零 LLM) ----------
# 住址/门牌要能命中"3号宿舍楼412室"这种写法。原模式只看"路/号院/小区"这类词, 漏掉了
# 社团报名通知里最常见的楼栋房间号(实测漏检, 见 §4.3 涉隐私)。这里补一条"位置词 +
# 数字门牌"的模式, 并要求出现 楼/栋/号/室, 以免把"大学生活动中心"这类正常场地名误判。
_ROOM_RE = re.compile(
    r"(?:(?:宿舍|公寓|教学楼|实验楼|行政楼|办公楼|综合楼|实训楼|科技楼)"
    r"[\u4e00-\u9fffA-Za-z0-9]{0,4}?(?:楼|栋)?\s*)?\d{1,4}\s*室"
    r"|\d{1,4}\s*(?:室|房间)")
_ADDR_RE = re.compile(
    r"[\u4e00-\u9fff]{2,10}(?:路|街|巷|道|号院|小区|公寓|宿舍楼|宿舍)"
    r"[\u4e00-\u9fff0-9]{0,8}?(?:号|栋|幢|室|楼)?")
_SENSITIVE_PATTERNS = (
    ("phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),                     # 手机号
    ("landline", re.compile(r"(?<!\d)0\d{2,3}-?\d{7,8}(?!\d)")),            # 固话
    ("id_card", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),                   # 身份证
    ("student_no", re.compile(r"(?i)(?<![A-Za-z0-9])(?:20\d{2})\d{6,10}(?![0-9])")),  # 学号
    ("address", _ADDR_RE),                                                   # 住址/街道门牌
    ("room", _ROOM_RE),                                                      # 楼栋房间号
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("bank_card", re.compile(r"(?<!\d)\d{16,19}(?!\d)")),
)

# 职务一致性比对时, 只看这些"角色词"(人名+职务两处不一致才算矛盾)
_ROLE_LIKE = ("社长", "副社长", "部长", "副部长", "主席", "队长", "会长",
              "指导老师", "负责人", "主持人", "主讲人", "辅导员", "团支书")


def detect_sensitive(text: str) -> List[dict]:
    """命中隐私模式 → [{"pattern", "sample"}](sample 脱敏后只留形状)。"""
    t = text or ""
    hits: List[dict] = []
    for name, rx in _SENSITIVE_PATTERNS:
        m = rx.search(t)
        if m:
            raw = m.group(0)
            hits.append({"pattern": name, "sample": _mask(raw)})
    return hits


def _mask(s: str) -> str:
    """脱敏展示: 保留首尾各 1-2 字符, 中间打码(审计与前端都只看得到形状)。"""
    if len(s) <= 4:
        return s[0] + "*" * (len(s) - 1)
    return f"{s[:2]}{'*' * (len(s) - 4)}{s[-2:]}"


# ---------- 冲突检测(素材库内部, 与稿件无关) ----------
def _metric_conflicts(materials: List[dict], window: Optional[dict] = None) -> List[dict]:
    """同指标不同值 → 冲突(design/11 §4.3 R3)。

    只在**同一活动窗口**内比: 往届活动的 200 人和今年活动的 183 人不是矛盾
    (design/11 §10 的历史层语料正是靠这一点避免误报)。
    """
    buckets: Dict[str, List[dict]] = {}
    for m in materials:
        if window and not in_window(m, window):
            continue
        for metric in extract_metrics(m.get("content") or ""):
            if metric.value is None:
                continue
            buckets.setdefault(metric.key, []).append(
                {"eid": m.get("eid"), "value": metric.value, "unit": metric.unit,
                 "authority": float(m.get("authority") or 0.0),
                 "source": m.get("source") or "",
                 "text": (m.get("content") or "")[:160]})
    out: List[dict] = []
    for key, rows in buckets.items():
        vals = {r["value"] for r in rows}
        if len(vals) < 2:
            continue
        rows_sorted = sorted(rows, key=lambda r: (-r["authority"], str(r["value"])))
        out.append({
            "kind": CONFLICT, "rule": "R3", "metric_key": key,
            "claim": f"{key}:{'|'.join(str(v) for v in sorted(vals))}",
            "assertion": " / ".join(f"{r['value']}{r['unit'] or ''}" for r in rows_sorted),
            "reason": (f"同一指标「{key}」在素材里有 {len(vals)} 个不同值: "
                       + "、".join(f"{r['value']}{r['unit'] or ''}({r['source']})"
                                   for r in rows_sorted)),
            "candidates": rows_sorted,
            "preferred_eid": rows_sorted[0]["eid"],
        })
    return out


def _period_conflicts(materials: List[dict]) -> List[dict]:
    """同主体时间说法不符 → 冲突(R2)。排除往届(时间差一年以上的不算矛盾)。"""
    by_subject: Dict[str, List[Tuple[dict, Period]]] = {}
    for m in materials:
        p = Period.parse(m.get("content") or "")
        subj = (m.get("subject") or "").strip()
        if p is None or not p.start or not subj:
            continue
        by_subject.setdefault(subj, []).append((m, p))
    out: List[dict] = []
    for subj, rows in by_subject.items():
        starts = {p.start for _, p in rows}
        if len(starts) < 2:
            continue
        if _spans_years(starts):
            continue                      # 跨年 = 往届 vs 今年, 不是矛盾
        rows_sorted = sorted(rows, key=lambda r: -float(r[0].get("authority") or 0.0))
        out.append({
            "kind": CONFLICT, "rule": "R2", "metric_key": "period",
            "claim": f"period:{subj}:{'|'.join(sorted(starts))}",
            "assertion": " / ".join(p.raw or p.start for _, p in rows_sorted),
            "reason": (f"「{subj}」的时间说法不一致: "
                       + "、".join(f"{p.raw or p.start}({m.get('source')})" for m, p in rows_sorted)),
            "candidates": [{"eid": m.get("eid"), "value": p.start, "unit": p.raw,
                            "authority": float(m.get("authority") or 0.0),
                            "source": m.get("source") or "",
                            "text": (m.get("content") or "")[:160]} for m, p in rows_sorted],
            "preferred_eid": rows_sorted[0][0].get("eid"),
        })
    return out


def _spans_years(starts: Iterable[str]) -> bool:
    """起点年份跨度 > 0 → 视为跨届(往届 vs 今年), 不判矛盾。"""
    years = {str(s)[:4] for s in starts if s}
    return len(years) > 1


def _role_conflicts(materials: List[dict]) -> List[dict]:
    """同一人两处职务不一致 → 冲突(design/11 §4.3)。"""
    by_person: Dict[str, List[Tuple[dict, str]]] = {}
    for m in materials:
        text = m.get("content") or ""
        for ent in extract_entities(text, None):
            if ent["kind"] != "person":
                continue
            # 找该人名附近 8 字内的角色词
            pos = text.find(ent["text"])
            near = text[max(0, pos - 8): pos + len(ent["text"]) + 8]
            for role in _ROLE_LIKE:
                if role in near:
                    by_person.setdefault(ent["text"], []).append((m, role))
                    break
    out: List[dict] = []
    for person, rows in by_person.items():
        roles = {r for _, r in rows}
        if len(roles) < 2:
            continue
        rows_sorted = sorted(rows, key=lambda r: -float(r[0].get("authority") or 0.0))
        out.append({
            "kind": CONFLICT, "rule": "R5", "metric_key": "entity",
            "claim": f"entity:role:{person}:{'|'.join(sorted(roles))}",
            "assertion": " / ".join(sorted(roles)),
            "reason": (f"{person}的职务有两处不一致: " + "、".join(
                f"{r}({m.get('source')})" for m, r in rows_sorted)),
            "candidates": [{"eid": m.get("eid"), "value": r, "unit": None,
                            "authority": float(m.get("authority") or 0.0),
                            "source": m.get("source") or "",
                            "text": (m.get("content") or "")[:160]} for m, r in rows_sorted],
            "preferred_eid": rows_sorted[0][0].get("eid"),
        })
    return out


def detect_conflicts(materials: List[dict],
                     window: Optional[dict] = None) -> List[dict]:
    """素材库内部的三类矛盾扫描(R2 时间 / R3 指标 / R5 职务)。"""
    return (_metric_conflicts(materials, window) + _period_conflicts(materials)
            + _role_conflicts(materials))


# ---------- 时间窗口与过期 ----------
def activity_window(materials: List[dict], now: Optional[str] = None) -> dict:
    """本轮活动窗口 = 素材库里**最新**的 period 起点(design/11 §4.3 stale 判定的锚点)。

    这是确定性口径: 最新的一批素材代表"今年这场活动", 比它还早一年以上的
    period 就是往届信息。now 参数只在素材库完全没有 period 时兜底(取年份)。
    """
    starts = []
    for m in materials:
        p = Period.parse(m.get("content") or "") or Period.from_dict(m.get("period"))
        if p is not None and p.start:
            starts.append(p.start)
    if starts:
        return {"current": max(starts), "source": "materials"}
    y = (now or "")[:4]
    if y.isdigit():
        return {"current": f"{y}-01", "source": "now"}
    return {"current": None, "source": "empty"}


def in_window(m: dict, window: dict) -> bool:
    """素材是否落在本轮窗口内(过期素材不参与矛盾比对)。"""
    cur = (window or {}).get("current")
    if not cur:
        return True
    p = Period.parse(m.get("content") or "") or Period.from_dict(m.get("period"))
    if p is None or not p.start:
        return True                      # 无时间的素材视为在窗口内
    return str(p.start)[:4] >= str(cur)[:4]


def is_stale(material: dict, window: dict) -> bool:
    """period 早于本轮活动窗口 → 过期(design/11 §4.3; 按年比较, 避免月差误报)。"""
    cur = (window or {}).get("current")
    if not cur:
        return False
    p = (Period.from_dict(material.get("period")) if material.get("period")
         else Period.parse(material.get("content") or ""))
    if p is None or not p.start:
        return False
    return str(p.start)[:4] < str(cur)[:4]


def is_isolated(material: dict, materials: List[dict]) -> bool:
    """孤证: 只有单一低权威来源, 且没有第二来源说同一件事(design/11 §4.3)。

    用户勾选 verified(主办方已确认)的素材不算孤证——这是"人在环里"的体现。
    """
    if float(material.get("authority") or 0.0) >= LOW_AUTHORITY:
        return False
    if bool(material.get("verified")):
        return False
    subject = (material.get("subject") or "").strip()
    content = material.get("content") or ""
    for other in materials:
        if other.get("eid") == material.get("eid"):
            continue
        if subject and _shares_subject(subject, other):
            return False
        if _text_overlap(content, other.get("content") or "") >= 0.6:
            return False
    return True


def _shares_subject(subject: str, other: dict) -> bool:
    from graphstore.types import subject_similarity
    if subject_similarity(subject, other.get("subject") or "") >= 0.9:
        return True
    return subject_similarity(subject, other.get("content") or "") >= 0.9


def _text_overlap(a: str, b: str, min_run: int = 10) -> float:
    """两段文本是否在说同一件事: 需要**连续** min_run 个汉字相同, 且片段内**没有数字**。

    为什么不用二字组 Jaccard: 社团活动的素材都在讲同一场活动, 用 2-gram 会让
    "现场气氛特别好"和"现场提供志愿者支持"互相"作证", 孤证就永远测不出来。
    为什么要排除含数字的片段: "手机13812345678" 与 "编号13812345678" 会共享整串
    数字, 但那是同一串数字的两次出现, 不构成两个独立来源。
    """
    from graphstore.types import norm_text
    na, nb = norm_text(a), norm_text(b)
    if not na or not nb:
        return 0.0
    short, long_ = (na, nb) if len(na) <= len(nb) else (nb, na)
    need = min(min_run, max(4, len(short) * 3 // 5))
    for size in range(len(short), need - 1, -1):
        for i in range(0, len(short) - size + 1):
            piece = short[i:i + size]
            if any(ch.isdigit() for ch in piece):
                continue
            if piece in long_:
                return size / max(1, len(short))
    return 0.0


# ---------- 段落核验 ----------
def verify_segment(seg: dict, materials: List[dict],
                   window: Optional[dict] = None) -> dict:
    """核验一个稿件段落 → 断言级结论 + 缺口 + cites 边(design/11 §5.1 流程图)。

    Returns:
      {
        "seg_id", "claims": [断言 + status(supported|conflict|no_source|sensitive)
                             + supports[] + gap?],
        "gaps": [缺口(含 claim 原句与 source_eid, 供待核实清单指回原文)],
        "cites": [边(dict)],
        "block": bool,      # 有阻断项 → 不许进入多平台适配
      }
    """
    seg_id = seg.get("seg_id") or seg.get("id") or "seg_?"
    text = seg.get("text") or ""
    mats = list(materials or [])
    claims = segment_claims(seg, mats)
    internal_conflicts = detect_conflicts([m for m in mats if in_window(m, window or {})],
                                          window)
    # 段落自报的来源(成稿时登记): 断言命中其中一条即视为"有据但不独立"
    declared = [eid for eid in (seg.get("source_refs") or [])]

    out_claims: List[dict] = []
    gaps: List[dict] = []
    cites: List[dict] = []

    def add_gap(gtype: str, claim: dict, reason: str, source_eid: str = "",
                extra: Optional[dict] = None) -> None:
        gap = {
            "event_id": source_eid or "",
            "weakness_type": gtype,
            "claim": claim.get("raw") or text,
            "assertion": claim.get("mention") or str(claim.get("value") or ""),
            "claim_key": claim_key(claim),
            "source_eid": source_eid or "",
            "seg_id": seg_id,
            "priority": GAP_PRIORITY.get(gtype, 0.5),
            "reason": reason,
            "blocking": gtype in BLOCKING_TYPES,
        }
        if extra:
            gap.update(extra)
        gaps.append(gap)

    for claim in claims:
        sup = find_support(claim, mats)
        eid_of_claim = _owning_eid(claim, mats, declared)
        # 矛盾优先于无据: 同指标不同值不是"查不到", 而是"两种说法"
        conf = _matching_conflict(claim, internal_conflicts)
        if conf is not None and not _settled_by_majority(claim, conf, mats):
            cands = conf.get("candidates") or []
            for c in cands[:2]:
                if c.get("eid"):
                    cites.append({"key": f"{seg_id}>{c['eid']}", "src": seg_id,
                                  "tgt": c["eid"], "kind": "conflict",
                                  "weight": 0.5, "claim": claim_key(claim),
                                  "assertion": claim.get("mention") or "",
                                  "similarity": 0.5})
            out_claims.append({**claim, "status": CONFLICT, "supports": cands,
                               "conflict": conf})
            add_gap(CONFLICT, claim,
                    conf.get("reason") or "素材之间说法不一致, 需用户裁决",
                    source_eid=cands[0].get("eid") if cands else "",
                    extra={"candidates": cands, "rule": conf.get("rule"),
                           "preferred_eid": conf.get("preferred_eid")})
            continue
        if conf is not None and not sup:
            # 落在一组矛盾上、但这个值谁也没说过 → 是"无据"(不是"两种说法")
            out_claims.append({**claim, "status": NO_SOURCE, "supports": []})
            if _reportable_no_source(claim):
                add_gap(NO_SOURCE, claim,
                        "该数值不在素材给出的说法之中, 需核实或删改",
                        source_eid=eid_of_claim or "")
            continue

        sens = detect_sensitive(claim.get("raw") or text) if claim.get("kind") == "entity" \
            else detect_sensitive(claim.get("mention") or "")
        if sens:
            out_claims.append({**claim, "status": SENSITIVE, "supports": sup,
                               "sensitive": sens})
            add_gap(SENSITIVE, claim,
                    "命中隐私模式: " + "、".join(h["pattern"] for h in sens),
                    source_eid=eid_of_claim or "",
                    extra={"sensitive": sens})
            continue

        if sup:
            for s in sup:
                cites.append({"key": f"{seg_id}>{s['eid']}", "src": seg_id,
                              "tgt": s["eid"], "kind": claim.get("kind"),
                              "weight": s["similarity"], "claim": claim_key(claim),
                              "assertion": claim.get("mention") or "",
                              "similarity": s["similarity"]})
            out_claims.append({**claim, "status": "supported", "supports": sup})
            # 孤证: 只有低权威单源, 且无第二来源(警告, 不阻断)
            low_only = all(float(s.get("authority") or 0.0) < LOW_AUTHORITY
                           for s in sup)
            verified = any(s.get("verified") for s in sup)
            if low_only and len(sup) == 1 and not verified:
                add_gap(ISOLATED, claim,
                        "仅出自单一低权威来源, 建议补官方来源",
                        source_eid=sup[0]["eid"],
                        extra={"candidates": sup})
            continue

        # 无支撑: 先看是不是"稿件在引用往届信息"——那就直接见过期, 而不是无据
        # (设计意图: 往届信息不是编的, 只是不能当今年的事实用, 见 §4.3 stale)
        stale_ref = _stale_reference(claim, mats, window or {})
        if stale_ref is not None:
            out_claims.append({**claim, "status": STALE, "supports": [stale_ref]})
            add_gap(STALE, claim,
                    f"引用了往届素材({stale_ref['text'][:40]}), 早于本轮活动窗口, "
                    "勿直接引用", source_eid=stale_ref["eid"],
                    extra={"candidates": [stale_ref]})
            continue

        out_claims.append({**claim, "status": NO_SOURCE, "supports": []})
        if _reportable_no_source(claim):
            add_gap(NO_SOURCE, claim,
                    "素材库找不到支撑句, 需核实或删改", source_eid=eid_of_claim or "")

    # 段落级隐私扫描(独立于其他缺口: 一段话里同时有矛盾和手机号, 两件都要报)
    seg_sens = detect_sensitive(text)
    if seg_sens:
        already = {h["sample"] for g in gaps for h in (g.get("sensitive") or [])}
        fresh = [h for h in seg_sens if h["sample"] not in already]
        if fresh:
            pseudo = {"kind": "text", "key": "sensitive", "value": fresh[0]["sample"],
                      "raw": text, "mention": fresh[0]["pattern"]}
            add_gap(SENSITIVE, pseudo,
                    "段落命中隐私模式: " + "、".join(h["pattern"] for h in fresh),
                    extra={"sensitive": fresh})

    return {"seg_id": seg_id, "text": text, "claims": out_claims, "gaps": gaps,
            "cites": cites, "block": any(g["blocking"] for g in gaps)}


def _owning_eid(claim: dict, materials: List[dict], declared: List[str]) -> str:
    """断言最可能的来源素材(用于待核实清单"指回原文")。

    口径: 段落自报来源里与断言相似度最高的那条; 都没有就取素材库中相似度最高者;
    再没有则空串(清单里显示为"无来源")。
    """
    _, _, _ = claim, materials, declared      # 形参保留以稳定签名
    from content.material_ops import find_support as _fs
    sup = _fs(claim, materials)
    if sup:
        return sup[0]["eid"] or ""
    for eid in declared:
        return eid or ""
    return ""


def _matching_conflict(claim: dict, conflicts: List[dict]) -> Optional[dict]:
    """断言是否落在某个已检出矛盾上。

    必须**指标键相同 + 断言的数值就是矛盾双方之一**: 稿件写"到场超过300人"而素材里
    只有 200 与 183 时, 那不是"两种说法冲突", 而是"这个数字没人说过" —— 属无据,
    该阻断但不该拉用户去二选一(design/11 §4.3: 矛盾 = 并列两个版本由用户选)。
    """
    k = claim.get("kind")
    for conf in conflicts:
        mk = conf.get("metric_key")
        if k == "metric" and mk == claim.get("key"):
            if _value_in_candidates(claim.get("value"), conf.get("candidates")):
                return conf
        elif k == "period" and mk == "period":
            return conf
        elif k == "entity" and mk == "entity":
            return conf
    return None


def _value_in_candidates(value, candidates: Optional[List[dict]]) -> bool:
    """断言的数值是否就是矛盾双方之一(200 命中 {200,183}; 300 不命中)。"""
    if value is None:
        return False
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    for c in candidates or []:
        try:
            cv = float(c.get("value"))
        except (TypeError, ValueError):
            continue
        if abs(cv - v) <= max(1.0, abs(cv) * 0.02):
            return True
    return False


def _stale_reference(claim: dict, materials: List[dict],
                     window: dict) -> Optional[dict]:
    """段落这条断言是不是"在说往届的事"(往届素材里有对应说法且已过期)。

    判定: 时间值与该素材时段的**月份**一致(2024-11 与 11-16 / 2024-11 都算),
    或者专名/评价表述在该素材里逐字出现。命中即视为"引用往届", 报 stale 而非无据。
    """
    if not (window or {}).get("current"):
        return None
    cv = str(claim.get("value") or "")
    for m in materials:
        if not is_stale(m, window):
            continue
        if claim.get("kind") == "period":
            p = Period.parse(m.get("content") or "") or Period.from_dict(m.get("period"))
            if p is not None and p.start and _same_period(p.start, cv):
                return _as_candidate(m)
        elif claim.get("kind") in ("entity", "statement"):
            ok, sim, _why = claim_in_text(claim, m.get("content") or "")
            if ok and sim >= 0.9:
                return _as_candidate(m)
    return None


def _month_key(v: str) -> str:
    """把时间值裁成"月份+日"用于跨届比对。

    "2024-11" / "2024-11-16" → "11" / "11-16"; "11-16" 原样保留。
    (往届通知写 2024年11月16日 → 解析成 2024-11; 段落写 2024年11月16日 → 同样是 2024-11;
     但素材二写 11月16日 → 解析成 11-16, 两者比"11"就应当命中。)
    """
    s = str(v or "")
    parts = [p for p in re.split(r"[-.]", s) if p]
    if len(parts) >= 2 and len(parts[0]) == 4:
        return "-".join(parts[1:3]) if len(parts) >= 3 else parts[1]
    return "-".join(parts[:2]) if len(parts) >= 2 else s


def _same_period(a: str, b: str) -> bool:
    """"2024-11" 与 "11-16" 视为同一场活动(月份一致, 或一方只有月份)。"""
    ka, kb = _month_key(a), _month_key(b)
    if ka == kb:
        return True
    ma = ka.split("-")[0]
    mb = kb.split("-")[0]
    return bool(ma) and ma == mb


def _as_candidate(m: dict) -> dict:
    return {"eid": m.get("eid"), "authority": float(m.get("authority") or 0.0),
            "source": m.get("source") or "", "text": (m.get("content") or "")[:160],
            "similarity": 1.0, "reason": "往届素材"}


def _reportable_no_source(claim: dict) -> bool:
    """哪些断言缺支撑时值得报 `no_source`(design/11 §4.3)。

    三类不报, 因为它们不是"稿件编了话", 而是抽取器的产物或口径不足:
      - entity: 人名/职务/场地没在素材里出现, 是素材覆盖问题, 交人工判断即可;
      - period 且值为 "MM-DD": 素材只写了月份时无法做日级比对, 报了全是噪音
        (真正的"时间说法不一致"由 detect_conflicts 的 R2 负责, 那里是矛盾的);
    数字与评价性表述是主战场, 一律报。
    """
    kind = claim.get("kind")
    if kind == "entity":
        return False
    if kind == "period" and re.fullmatch(r"\d{2}-\d{2}", str(claim.get("value") or "")):
        return False
    return True


# ---------- 整稿核验 ----------
def verify_draft(draft: dict, materials: List[dict],
                 window: Optional[dict] = None,
                 user_resolutions: Optional[List[dict]] = None) -> dict:
    """整稿核验: 逐段核验 + 汇总缺口 + 门禁判定(design/11 §9 验收 A2/A5)。

    user_resolutions: 用户已裁决的矛盾([{claim_key, chosen_eid}]) —— 传进来才能让
    "用户选了哪一版"在重跑核验后依然有效(否则每次核验都会把同一矛盾摆回来)。

    Returns: {segments, gaps, cites, block, summary:{by_type, counts, blocking}}
    """
    segs = draft.get("segments") or []
    mats = list(materials or [])
    if window is None:
        window = activity_window(mats)
    out_segs: List[dict] = []
    gaps: List[dict] = []
    cites: List[dict] = []
    for seg in segs:
        r = verify_segment(seg, mats, window)
        out_segs.append(r)
        gaps.extend(r["gaps"])
        cites.extend(r["cites"])

    # 段落级缺口按 (类型, 断言标识) 去重: 同一句在多个段落里出现时只报一次
    gaps = _dedupe_gaps(gaps)

    # 素材级缺口: 矛盾 / 涉隐私 / 过期 / 孤证(它们不属于某句话, 属于某条素材)
    # 段落级已经报过的按 (类型, 断言标识) 去重, 所以这里不会重复计数。
    # 门禁口径: **段落级**矛盾/无据/涉隐私阻断(稿件里真有这句话才算), 素材级只进
    # 待核实清单与素材库自检 —— 不能因为素材库里存在一对矛盾, 就把所有稿子都拦下。
    for conf in detect_conflicts([m for m in mats if in_window(m, window)], window):
        cands = conf.get("candidates") or []
        gaps.append({
            "event_id": (cands[0] or {}).get("eid") if cands else "",
            "source_eid": (cands[0] or {}).get("eid") if cands else "",
            "weakness_type": CONFLICT, "claim": conf.get("reason") or "",
            "assertion": conf.get("assertion") or "",
            "claim_key": conf.get("claim") or conf.get("reason") or "",
            "seg_id": "", "priority": GAP_PRIORITY[CONFLICT], "blocking": False,
            "reason": conf.get("reason") or "素材之间说法不一致",
            "candidates": cands, "rule": conf.get("rule"),
            "material_level": True,
        })
    for m in mats:
        for hit in detect_sensitive(m.get("content") or ""):
            gaps.append({
                "event_id": m.get("eid") or "", "source_eid": m.get("eid") or "",
                "weakness_type": SENSITIVE, "claim": m.get("content") or "",
                "assertion": hit["sample"],
                "claim_key": f"sensitive:{m.get('eid')}:{hit['sample']}",
                "seg_id": "", "priority": GAP_PRIORITY[SENSITIVE], "blocking": False,
                "reason": f"素材命中隐私模式: {hit['pattern']}(成稿时不许写进稿件)",
                "sensitive": [hit], "material_level": True,
            })
        if is_stale(m, window):
            gaps.append({
                "event_id": m.get("eid") or "", "source_eid": m.get("eid") or "",
                "weakness_type": STALE, "claim": m.get("content") or "",
                "assertion": (m.get("period") or {}).get("raw", "") if isinstance(
                    m.get("period"), dict) else "",
                "claim_key": f"stale:{m.get('eid')}", "seg_id": "",
                "priority": GAP_PRIORITY[STALE], "blocking": False,
                "reason": (f"往届信息({_period_raw(m)}), 早于本轮活动窗口 "
                           f"{window.get('current')}, 勿直接引用"),
            })
        if is_isolated(m, mats):
            gaps.append({
                "event_id": m.get("eid") or "", "source_eid": m.get("eid") or "",
                "weakness_type": ISOLATED, "claim": m.get("content") or "",
                "assertion": m.get("subject") or "",
                "claim_key": f"isolated:{m.get('eid')}", "seg_id": "",
                "priority": GAP_PRIORITY[ISOLATED], "blocking": False,
                "reason": "仅出自单一低权威来源且无第二来源, 建议补官方来源",
            })

    # 矛盾: 用户裁决优先, 否则按来源权威度给出裁定结果(裁定后不再是阻断项)
    gaps = resolve_conflicts(gaps, mats, user_resolutions)
    # 全量排序(不截断): 核验报告要列全五类缺口; 截前 3 只用于决策链"先补哪几段"
    ordered = top_weak_sorted(gaps, top=0)
    by_type = {t: 0 for t in GAP_TYPES}
    for g in ordered:
        by_type[g["weakness_type"]] = by_type.get(g["weakness_type"], 0) + 1
    return {
        "segments": out_segs,
        "gaps": ordered,
        "cites": cites,
        "block": any(g.get("blocking") for g in ordered),
        "summary": {"by_type": by_type, "total": len(ordered),
                    "blocking": sum(1 for g in ordered if g.get("blocking")),
                    "window": window},
    }


def is_conflicting_claim(claim: dict, materials: List[dict],
                         window: Optional[dict] = None) -> bool:
    """这条断言是否属于**尚未裁定**的矛盾(供成稿引擎判定"这段要不要并列两版")。

    口径与核验器一致(design/11 §4.3):
      - 断言的值有多条素材支持 → 有据可依, 不是矛盾;
      - 断言的只被一部分素材支持、另一部分给出别的值 → 矛盾(需用户选);
      - 断言的值谁都没说过 → 不是矛盾, 是无据。
    """
    mats = list(materials or [])
    if window is None:
        window = activity_window(mats)
    confs = detect_conflicts([m for m in mats if in_window(m, window)], window)
    conf = _matching_conflict(claim, confs)
    if conf is None:
        return False
    if claim.get("kind") != "metric":
        return True
    return not _settled_by_majority(claim, conf, mats)


def _supported_eids(claim: dict, materials: List[dict]) -> List[str]:
    """支持该断言的素材 eid(与核验器同一判定: 同指标同值)。"""
    return [s["eid"] for s in find_support(claim, materials) if s.get("eid")]


def _settled_by_majority(claim: dict, conf: dict, materials: List[dict]) -> bool:
    """这个值是否**有据可依**(多数素材都这么说) → 不再是"两种说法"。

    判定口径(确定性, 零 LLM):
      - 候选值只覆盖到的素材数 < 2, 或这个值被至少两方素材支持 → 有据;
      - 候选值有 2 个、两边各一条素材(势均力敌) → 也视为有据, 但由
        `_resolve_conflicts` 打上"已按权威度裁定、可再协商"的标记(design/11 §11
        协作分: 矛盾项并列展示, 用户可点选官方口径)。
      - 其余情况(这个值只有一条低权威来源、另一方有多条) → 仍需用户裁决。
    """
    cands = conf.get("candidates") or []
    if len(cands) < 2:
        return False
    supported = _supported_eids(claim, materials)
    hit = sum(1 for c in cands if _value_in_candidates(claim.get("value"), [c]))
    if hit >= 2:
        return True
    if hit == 1 and len(cands) == 2:
        return True                       # 势均力敌: 记裁定结果 + 保留并列候选
    if supported:
        others = [c.get("eid") for c in cands if c.get("eid") not in supported]
        return not others or len(supported) >= len(cands)
    return False


def resolve_conflicts(gaps: List[dict], materials: List[dict],
                      user_resolutions: Optional[List[dict]] = None) -> List[dict]:
    """给每个矛盾缺口记下**裁定结果**与并列候选, 供前端"点选官方口径"。

    优先级: **用户裁决 > 按来源权威度裁定**。
    - 用户裁决(`user_resolutions`: [{claim_key, chosen_eid}])一旦存在就永远优先,
      并且**跨核验轮次有效** —— 否则用户选完、系统重跑核验又会把同一矛盾摆回来;
    - 裁定不等于替用户决定: 仍并列展示两个版本, 用户可随时改选。
    """
    by_key = {r.get("claim_key"): r for r in (user_resolutions or []) if r.get("claim_key")}
    out: List[dict] = []
    for g in gaps:
        if g.get("weakness_type") != CONFLICT:
            out.append(g)
            continue
        cands = sorted(g.get("candidates") or [],
                       key=lambda c: (-float(c.get("authority") or 0.0),
                                      str(c.get("value"))))
        if not cands:
            out.append(g)
            continue
        g = dict(g)
        user = by_key.get(g.get("claim_key"))
        if user:
            chosen = next((c for c in cands if c.get("eid") == user.get("chosen_eid")),
                          cands[0])
            g.update({
                "resolved": True,
                "resolution": "user",
                "chosen": chosen,
                "chosen_eid": chosen.get("eid"),
                "candidates": cands,
                "resolution_note": (f"由用户选定「{chosen.get('source')}」版本 "
                                    f"{chosen.get('value')}{chosen.get('unit') or ''}; "
                                    "可随时改选"),
                "blocking": False,
            })
            out.append(g)
            continue
        chosen = cands[0]
        g.update({
            "resolved": True,
            "resolution": "authority",
            "chosen": chosen,
            "chosen_eid": chosen.get("eid"),
            "candidates": cands,
            "resolution_note": (f"按来源权威度取「{chosen.get('source')}"
                                f"({chosen.get('authority')})」版本 "
                                f"{chosen.get('value')}{chosen.get('unit') or ''}; "
                                f"用户可点选另一版本覆盖"),
            "blocking": False,
        })
        out.append(g)
    return out


def _dedupe_gaps(gaps: List[dict]) -> List[dict]:
    """同一 (类型, 断言标识, 来源素材) 的缺口只留一条(取 priority 最高者)。"""
    best: Dict[tuple, dict] = {}
    order: List[tuple] = []
    for g in gaps:
        key = (g.get("weakness_type"), g.get("claim_key"), g.get("assertion"),
               g.get("source_eid"))
        if key not in best:
            best[key] = g
            order.append(key)
        elif float(g.get("priority") or 0) > float(best[key].get("priority") or 0):
            best[key] = g
    return [best[k] for k in order]


def _period_raw(m: dict) -> str:
    p = Period.parse(m.get("content") or "")
    return (p.raw if p else "") or str((m.get("period") or {}).get("raw") or "")


def top_weak_sorted(gaps: List[dict], top: int = 3) -> List[dict]:
    """缺口排序: 类型优先级升序 → 同级按 priority 降序(design/11 §4.3)。

    top>0 时只截取前 top 条用于"决策链补段"; 校验/清单场景传 top=0 取全量。
    """
    ordered = sorted(gaps, key=lambda g: (GAP_ORDER.get(g.get("weakness_type"), 9),
                                          -float(g.get("priority") or 0.0)))
    return ordered[:top] if top else ordered


def blocking_gaps(gaps: List[dict]) -> List[dict]:
    """阻断项(不许进入多平台适配)。"""
    return [g for g in gaps if g.get("blocking")]


def check_list(gaps: List[dict]) -> List[dict]:
    """《待核实清单》: 每条缺口带上"指得回原文"的三件事(句/素材/原因)。

    矛盾项额外带**裁定状态**: `resolved` / `resolution`(user=用户选的, authority=系统
    按来源权威度先裁定的) / `chosen_eid` / `resolution_note`。前端据此把两个候选版本
    并排列出、在被采用的那一版打"当前采用", 另一版保留"选它"可改选
    (design/11 §11 协作分: 矛盾由用户裁决, 裁定不等于替用户决定)。
    """
    out = []
    for g in top_weak_sorted(gaps, top=0):
        row = {
            "weakness_type": g.get("weakness_type"),
            "label": GAP_LABELS.get(g.get("weakness_type"), g.get("weakness_type")),
            "assertion": g.get("assertion") or "",
            "claim": (g.get("claim") or "")[:200],
            "source_eid": g.get("source_eid") or "",
            "seg_id": g.get("seg_id") or "",
            "reason": g.get("reason") or "",
            "blocking": bool(g.get("blocking")),
            "candidates": g.get("candidates") or [],
        }
        if g.get("weakness_type") == CONFLICT:
            row.update({
                "resolved": bool(g.get("resolved")),
                "resolution": g.get("resolution") or "",
                "chosen_eid": g.get("chosen_eid") or "",
                "resolution_note": g.get("resolution_note") or "",
            })
        out.append(row)
    return out


GAP_LABELS = {
    NO_SOURCE: "无据", CONFLICT: "矛盾", SENSITIVE: "涉隐私",
    ISOLATED: "孤证", STALE: "过期",
}


__all__ = [
    "GAP_ORDER", "GAP_PRIORITY", "GAP_LABELS", "BLOCKING_TYPES", "WARNING_TYPES",
    "detect_sensitive", "detect_conflicts", "activity_window", "in_window",
    "is_stale", "is_isolated", "verify_segment", "verify_draft",
    "top_weak_sorted", "blocking_gaps", "check_list",
    "is_conflicting_claim", "resolve_conflicts",
]
