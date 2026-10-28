# coding: utf-8
"""content/render.py — 双轨渲染 + 断言级 diff(design/11 §5.4, 验收 A5)。

**轨一(默认, 零 LLM)**: 素材图 + 段结构 → 按平台规范渲染。它是**事实基线**。
**轨二(授权, LLM, 点 P24)**: 只在轨一结果上润色, prompt 硬约束:

    不得新增事实; 不得改动任何数字、人名、职务、时间、地点; 只调整语序、连接词与语气。

**验收 A5(真实性专测)**: 轨二产物与轨一产物做**断言级 diff** —— 数字集合与专名
集合必须完全一致, 不一致即判失败(§12 R9: 轨二润色篡改事实是明确风险)。
`assertion_diff()` 就是这条验收标准的实现: 比对走的是与事实核验**同一套抽取器**,
所以"能过核验的说法"和"能过 diff 的说法"是同一个口径。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from graphstore.types import Period, extract_metrics, norm_text

from content import platform_spec as spec
from content.material_ops import (
    SUBJECTIVE_MARKERS, extract_entities, extract_subjective,
)


# ---------- 轨一: 确定性渲染 ----------
# 稿件可以有两种来源(design/11 §7 工作台改版):
#   - 结构化稿: 系统成稿, 段带 lead/process/... 六类 kind;
#   - 积木流稿: 用户从左栏拖出来的自由顺序, 块没有 kind(一律 "block")。
# 后者不逼用户分段, 由下面这层按**位置**给 kind(第一块当导语、最后一块当结语),
# 让发布轨和预览轨对同一份稿子说同一件事。
ALL_PLATFORM_KINDS = frozenset(
    set(spec.SEGMENTS) | {"hook", "conflict", "proof", "cta",
                          "headline", "sub", "time_place"})
_POSITIONAL_MIDDLE = ("process", "highlight", "background", "impact")


def is_free_flow(segments: List[dict]) -> bool:
    """没有一段带平台认识的 kind → 视为积木流(位置映射)。"""
    return not any((s.get("kind") or "") in ALL_PLATFORM_KINDS for s in segments or [])


def positional_kinds(n: int) -> List[str]:
    """按位置给 kind: 第一块导语, 最后一块结语, 中间循环 process/highlight/background/impact。"""
    if n <= 0:
        return []
    if n == 1:
        return ["lead"]
    if n == 2:
        return ["lead", "closing"]
    kinds = ["lead"]
    middle = n - 2
    for i in range(middle):
        kinds.append(_POSITIONAL_MIDDLE[i % len(_POSITIONAL_MIDDLE)])
    kinds.append("closing")
    return kinds


def infer_kinds(segments: List[dict]) -> List[dict]:
    """积木流 → 带位置 kind 的段(不改文本、不改顺序)。"""
    segs = [dict(s) for s in (segments or [])]
    if not is_free_flow(segs):
        return segs
    kinds = positional_kinds(len(segs))
    for seg, kind in zip(segs, kinds):
        seg["kind"] = kind
        seg.setdefault("label", spec.SEGMENT_LABELS.get(kind, kind))
    return segs


def render_segments(segments: List[dict], platform: str,
                    materials: Optional[List[dict]] = None) -> dict:
    """轨一: 段结构 → 平台版本(零 LLM)。超字数只告警, 不静默截断。"""
    # 海报有独立的四元素排版(主体/一个数字/时间地点), 不走"段拼标题"那条路:
    # 走那条路的结果是整段正文硬裁到 60 字, 永远是 60/60 且 trimmed(结构缺陷)。
    if platform == "poster":
        from content.preview import poster_version
        return poster_version(segments)
    segments = infer_kinds(segments)
    sp = spec.platform_spec(platform)
    wanted = sp["segments"]
    by_kind: Dict[str, List[dict]] = {}
    for seg in segments or []:
        by_kind.setdefault(seg.get("kind") or "", []).append(seg)
    out_segs: List[dict] = []
    for kind in wanted:
        src = by_kind.get(kind) or []
        if not src and platform != "wechat":
            src = _fallback_for(kind, by_kind, segments)
        for seg in src:
            text = (seg.get("text") or "").strip()
            if not text:
                continue
            out_segs.append({
                "seg_id": seg.get("seg_id"),
                "kind": kind,
                "label": seg.get("label") or spec.SEGMENT_LABELS.get(kind, kind),
                "text": text,
                "source_refs": list(seg.get("source_refs") or []),
                "status": seg.get("status"),
            })
    title = _title(platform, segments, sp)
    text = _join(platform, title, out_segs)
    # 超上限时按**句子边界**收敛到上限内: 宁可少写一句, 不把句子截断
    trimmed = False
    if spec.over_limit(platform, text)["over"]:
        text, out_segs = _fit_to_limit(platform, title, out_segs, int(sp["max_chars"]))
        trimmed = True
    limit = spec.over_limit(platform, text)
    return {
        "platform": platform,
        "label": sp["label"],
        "track": "base",
        "track_label": "轨一·事实基线",
        "title": title,
        "segments": out_segs,
        "text": text,
        "limit": limit,
        "trimmed": trimmed,
        "shots": _shots(platform, out_segs),
        "llm_used": False,
    }


def _fit_to_limit(platform: str, title: str, segs: List[dict],
                  max_chars: int) -> tuple:
    """按段/句边界裁到字数上限内(不做句中截断), 返回 (text, 保留下来的段)。"""
    kept: List[dict] = []
    for seg in segs:
        trial = kept + [seg]
        if spec.char_count(_join(platform, title, trial)) <= max_chars:
            kept.append(seg)
            continue
        # 整段放不下 → 试着只留该段的第一句
        first = _first_sentence(seg.get("text") or "")
        if first and first != seg.get("text"):
            short = dict(seg, text=first)
            if spec.char_count(_join(platform, title, kept + [short])) <= max_chars:
                kept.append(short)
        break
    return _join(platform, title, kept), kept


def _first_sentence(text: str) -> str:
    import re
    parts = [p for p in re.split(r"[。！？!?；;]", text or "") if p.strip()]
    return parts[0].strip() if parts else ""


def _fallback_for(kind: str, by_kind: Dict[str, List[dict]],
                  segments: List[dict]) -> List[dict]:
    """短视频/海报的段名与稿件六段不同名, 用确定性映射补位(不新增内容)。

    shortvideo: hook←lead, conflict←highlight/process, proof←process, cta←closing
    poster:     headline←lead, sub←highlight, time_place←lead(取时间地点句)
    """
    map_ = PLATFORM_SEGMENT_SOURCE.get(kind, ())
    picked: List[dict] = []
    for src_kind in map_:
        picked.extend(by_kind.get(src_kind) or [])
    if not picked:
        picked = list(segments or [])[:1]
    if kind == "time_place":
        for seg in picked:
            if Period.parse(seg.get("text") or "") is not None:
                return [seg]
    return picked[:1]


# 平台段 ← 稿件段(确定性映射, 不引入新事实)
PLATFORM_SEGMENT_SOURCE = {
    "hook": ("lead",),
    "conflict": ("highlight", "process", "lead"),
    "proof": ("process", "highlight", "impact"),
    "cta": ("closing", "lead"),
    "headline": ("lead", "highlight"),
    "sub": ("highlight", "impact", "lead"),
    "time_place": ("lead", "background"),
}


def _title(platform: str, segments: List[dict], sp: dict) -> str:
    """标题: 取导语段的主题词(确定性, 不加形容词)。"""
    lead = None
    for seg in segments or []:
        if seg.get("kind") == "lead":
            lead = seg
            break
    base = (lead or {}).get("text") or (segments or [{}])[0].get("text") or ""
    base = base.strip().rstrip("。")
    if platform == "poster":
        return (lead or {}).get("subject") or base[:20]
    if platform == "shortvideo":
        return base[:24] or "活动速览"
    return base[:30] or "未命名稿件"


def _join(platform: str, title: str, segs: List[dict]) -> str:
    if platform == "poster":
        parts = [title] + [s["text"] for s in segs]
        return "\n".join(p for p in parts if p)
    if platform == "shortvideo":
        lines = [title]
        for s in segs:
            lines.append(f"[{s['label']}] {s['text']}")
        return "\n".join(lines)
    lines = [title]
    for s in segs:
        lines.append(f"\n{s['label']}\n{s['text']}")
    return "\n".join(lines)


def _shots(platform: str, segs: List[dict]) -> List[dict]:
    """短视频分镜(design/11 §5.4 shots=6): 用段落文字填分镜, 不生成新内容。"""
    if platform != "shortvideo":
        return []
    by_kind = {s["kind"]: s for s in segs}
    out = []
    for tpl in spec.SHOT_TEMPLATE:
        src_kinds = [k for k, nos in spec.SHOT_BY_SEGMENT.items() if tpl["no"] in nos]
        text = ""
        for k in src_kinds:
            if k in by_kind:
                text = by_kind[k]["text"]
                break
        out.append({**tpl, "text": text, "source_refs":
                    list((by_kind.get(src_kinds[0]) or {}).get("source_refs") or [])})
    return out


# ---------- 断言级 diff(验收 A5) ----------
def assertion_set(text: str, materials: Optional[List[dict]] = None) -> dict:
    """一段文本的"事实指纹": 数字集合 + 专名集合 + 时间集合 + 评价表述集合。

    抽取器与事实核验完全同一套(content.material_ops / graphstore.types),
    这是"断言级"而非"字面级"比对的关键: 改语序、换连接词不会改变指纹,
    改数字、换人名、动时间一定会变。
    """
    t = text or ""
    nums = sorted({f"{m.key}:{_num(m.value)}{m.unit or ''}" for m in extract_metrics(t)})
    ents = sorted({f"{e['kind']}:{e['text']}" for e in extract_entities(t, None)})
    p = Period.parse(t)
    periods = [f"{p.start}~{p.end or ''}"] if (p is not None and p.start) else []
    subs = sorted(set(extract_subjective(t)))
    return {"numbers": nums, "entities": ents, "periods": sorted(periods),
            "subjective": subs}


def _num(v) -> str:
    try:
        f = float(v)
        return str(int(f)) if f == int(f) else str(f)
    except (TypeError, ValueError):
        return str(v)


def assertion_diff(base_text: str, polished_text: str,
                   materials: Optional[List[dict]] = None) -> dict:
    """轨一 vs 轨二 的断言级 diff(验收 A5)。

    Returns: {ok, numbers, entities, periods, subjective, violations:[...]}
    ok=False 即"润色篡改了事实", 必须拒绝该润色结果(保留轨一)。
    """
    a = assertion_set(base_text, materials)
    b = assertion_set(polished_text, materials)
    diff = {}
    violations: List[str] = []
    for key, label in (("numbers", "数字/指标"), ("entities", "人名/职务/场地/机构"),
                       ("periods", "时间"), ("subjective", "评价表述")):
        added = [x for x in b[key] if x not in a[key]]
        removed = [x for x in a[key] if x not in b[key]]
        diff[key] = {"added": added, "removed": removed}
        if added:
            violations.append(f"轨二新增{label}: {'、'.join(added[:6])}")
        if removed:
            violations.append(f"轨二丢失{label}: {'、'.join(removed[:6])}")
    return {"ok": not violations, "diff": diff, "violations": violations,
            "base_fingerprint": a, "polished_fingerprint": b}


def violates_facts(base_text: str, polished_text: str) -> Tuple[bool, List[str]]:
    """便捷判定: (是否篡改事实, 违规说明)。"""
    r = assertion_diff(base_text, polished_text)
    return (not r["ok"]), r["violations"]


# ---------- 平台版本集合(同一稿 → N 平台) ----------
def render_all(segments: List[dict], drafts_platforms: Optional[List[str]] = None,
               materials: Optional[List[dict]] = None) -> Dict[str, dict]:
    """同一稿 → N 个平台版本(design/11 §5.5 第 5 步 platform_adapt)。"""
    out: Dict[str, dict] = {}
    for p in (drafts_platforms or spec.platforms()):
        out[p] = render_segments(segments, p, materials)
    return out


def export_package(base_text: str, platform_versions: Dict[str, dict],
                   topic: str = "") -> str:
    """一键导出"宣发包": 订阅号版 + 短视频脚本 + 海报文案(design/11 §11 宣发)。"""
    lines = [f"# 宣发包 · {topic or platform_versions.get('wechat', {}).get('title') or '稿件'}",
             "\n> 本包由系统确定性渲染生成(轨一·事实基线), 每个平台的文字都可点回素材。"]
    for p, v in platform_versions.items():
        lines.append(f"\n## {v.get('label') or p}({v.get('limit', {}).get('count', 0)}"
                     f"/{v.get('limit', {}).get('limit', '-')} 字)")
        lines.append(v.get("text") or "")
        if v.get("shots"):
            lines.append("\n### 分镜")
            for sh in v["shots"]:
                lines.append(f"- 镜{sh['no']} {sh['name']}({sh['camera']}, "
                             f"{sh['seconds']}s): {sh.get('text') or ''}")
    return "\n".join(lines)


__all__ = ["render_segments", "render_all", "export_package", "assertion_set",
           "assertion_diff", "violates_facts", "PLATFORM_SEGMENT_SOURCE"]
