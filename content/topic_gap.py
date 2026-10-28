# coding: utf-8
"""content/topic_gap.py — 缺口驱动的选题(design/11 §5.2, 零 LLM)。

三个维度的**确定性扫描**:

| 维度 | 判定 | 产出选题方向 |
|---|---|---|
| `no_draft`  素材多但无稿 | 某活动关联 activity 素材 ≥ 5 条, 但无对应稿件 | "这个活动该出稿了" |
| `thin_source` 有稿但素材薄 | 某稿件的 cites 边 < 3, 或 no_source 缺口 ≥ 2 | "先把依据补齐再发" |
| `no_current` 往届有今年无 | temporal 边关联的往届同类活动有稿, 今年无 | "这个系列活动今年还差一篇" |

排序: `缺口数 × 类型权重 × 素材权威度均值`, 取前 N 入选题库(design/11 §5.2)。
排序与筛选全部零 LLM; LLM 只在"出选题文案"(P8)时用到, 且是可选增强。
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Tuple

from graphstore.types import NO_SOURCE, Period, norm_text, subject_similarity

from content.material_ops import authority_mean

_RE_YEAR = re.compile(r"((?:19|20)\d{2})\s*年")

# 参与"同一场活动"归组的素材类型(背景/人物这类可以独立成组的除外)
EVENT_MATERIAL_TYPES = ("activity", "notice", "achievement")

# 三维度的类型权重(排序公式的因子; 数值取"素材多的成稿价值 > 补依据 > 系列补齐")
TOPIC_WEIGHTS = {"no_draft": 1.0, "thin_source": 0.8, "no_current": 0.6}
TOPIC_LABELS = {"no_draft": "素材多但无稿", "thin_source": "有稿但素材薄",
                "no_current": "往届有今年无"}

# 判定阈值(design/11 §5.2 表)
MIN_ACTIVITY_MATERIALS = 5      # 素材多但无稿: 关联素材条数下限
MIN_CITES = 3                   # 有稿但素材薄: cites 边下限
MIN_NO_SOURCE = 2               # 有稿但素材薄: no_source 缺口下限
TOP_N = 5                       # 入选题库的条数上限

# "同类活动"的判定: 主体名去掉届次词后的相似度
_STAGE_WORDS = ("上届", "本届", "今年", "去年", "第", "届", "次", "年")


def _series_key(subject: str) -> str:
    """系列活动名: 去掉届次词后的归一化串("上届编程马拉松比赛" → "编程马拉松比赛")。"""
    s = subject or ""
    for w in _STAGE_WORDS:
        s = s.replace(w, "")
    return norm_text(s)


def _drafts(draft_versions: Iterable[dict]) -> List[dict]:
    """稿件版本列表 → 只保留每个 topic 的最新一版(有稿/无稿判定按最新版)。"""
    best: Dict[str, dict] = {}
    for d in draft_versions or []:
        topic = (d.get("topic") or "").strip()
        if not topic:
            continue
        cur = best.get(topic)
        if cur is None or int(d.get("seq") or 0) >= int(cur.get("seq") or 0):
            best[topic] = d
    return list(best.values())


def _has_draft_for(subject: str, drafts: List[dict]) -> bool:
    """今年是否已有该主题的稿(design/11 §5.2 维度 1/3 的共用判定)。

    判定顺序(届次优先于相似度):
      1. 稿件主题里的**届次词**与待判主题不一致(例如稿是"上届…", 主题是"…") → 不算;
      2. 否则主题覆盖度 ≥0.9 → 算有稿;
      3. 否则系列名(去届次词)互相包含 → 算有稿。
    第 1 条是关键: "上届编程马拉松比赛" 与 "编程马拉松比赛" 字符覆盖度是 1.0,
    不先挡住届次, 往届的稿就会被当成今年的稿, "往届有今年无"永远测不出来。
    """
    sk = _series_key(subject)
    if not sk:
        return False
    for d in drafts:
        topic = d.get("topic") or ""
        if _stage_word(topic) and _stage_word(topic) != _stage_word(subject):
            continue
        if _topic_coverage(topic, subject) >= 0.9:
            return True
        dk = _series_key(topic)
        if dk and (dk == sk or sk in dk or dk in sk):
            return True
    return False


def _topic_coverage(a: str, b: str) -> float:
    """两个主题的**字符覆盖率**(min 侧): 只看词表覆盖会把"上届XX"与"XX"判成同一个。

    subject_similarity 的 2-gram 口径在包含关系上直接给 1.0, 用于判重没问题,
    但判"是不是同一届"会把往届与今年混为一谈, 所以这里另用字符覆盖率。
    """
    na, nb = norm_text(a), norm_text(b)
    if not na or not nb:
        return 0.0
    sa, sb = set(na), set(nb)
    return len(sa & sb) / max(1, min(len(sa), len(sb)))


def scan_topics(materials: List[dict], drafts: Optional[List[dict]] = None,
                cites: Optional[Dict[str, dict]] = None,
                gaps: Optional[List[dict]] = None,
                window: Optional[dict] = None,
                temporal: Optional[Dict[str, dict]] = None) -> List[dict]:
    """三维度扫描 → 选题候选列表(已排序)。

    materials: 素材图事件列表(events_payload 形状)
    drafts:    稿件版本列表([{topic, seq, seg_count, ...}])
    cites:     cites 边字典(store.latest()["edges"]["cites"])
    temporal:  temporal 边字典(往届 ↔ 今年 的活动时间线, design/11 §4.2)
    gaps:      最近一次核验的缺口列表
    """
    mats = list(materials or [])
    drafts = _drafts(drafts or [])
    cites = cites or {}
    temporal = temporal or {}
    gaps = gaps or []
    out: List[dict] = []

    # ---- 维度 1: 素材多但无稿 ----
    for grp in _group_by_series(mats):
        acts = [m for m in grp["materials"]
                if (m.get("type") or "") in ("activity", "notice", "achievement")]
        if len(acts) < MIN_ACTIVITY_MATERIALS:
            continue
        if _has_draft_for(grp["subject"], drafts):
            continue
        out.append(_topic("no_draft", grp["subject"],
                          f"该活动已关联 {len(acts)} 条素材但仍无稿件",
                          len(acts), authority_mean(grp["materials"]),
                          eids=[m.get("eid") for m in grp["materials"]]))

    # ---- 维度 2: 有稿但素材薄 ----
    taken = {_series_key(t.get("subject") or "") for t in out}
    for d in drafts:
        topic = d.get("topic") or ""
        # 往届稿件的"素材薄"不是当务之急(当年素材本来就不该挂到往届稿上), 跳过
        if not _is_current_topic(topic, mats, window):
            continue
        if _series_key(topic) in taken:
            continue                      # 维度 3 已就同一系列给出更贴切的选题
        eids = _cited_eids(topic, cites, d)
        n_cites = len(eids)
        no_src = sum(1 for g in gaps
                     if g.get("weakness_type") == NO_SOURCE
                     and _same_series(g.get("claim") or topic, topic))
        if n_cites >= MIN_CITES and no_src < MIN_NO_SOURCE:
            continue
        mats_of = [m for m in mats if m.get("eid") in eids] or mats
        reasons = []
        if n_cites < MIN_CITES:
            reasons.append(f"cites 溯源边只有 {n_cites} 条(下限 {MIN_CITES})")
        if no_src >= MIN_NO_SOURCE:
            reasons.append(f"无据缺口 {no_src} 条")
        out.append(_topic("thin_source", topic,
                          "先把依据补齐再发: " + "、".join(reasons),
                          max(1, n_cites + no_src), authority_mean(mats_of),
                          eids=list(eids)))

    # ---- 维度 3: 往届有今年无(走 temporal 边: 往届活动 ↔ 今年活动) ----
    by_eid = {m.get("eid"): m for m in mats if m.get("eid")}
    for edge in temporal.values():
        src, tgt = by_eid.get(edge.get("src")), by_eid.get(edge.get("tgt"))
        if src is None or tgt is None:
            continue
        old, new = _order_by_year(src, tgt)
        if old is None or new is None:
            continue                      # 年份相同: 不是"往届 vs 今年"
        new_subj = (new.get("subject") or "").strip()
        new_grp = _group_containing(mats, new)
        # 用**届次组名**判"今年有没有稿"("2024年编程马拉松比赛" ≠ "编程马拉松比赛"),
        # 否则往届的稿会被当成今年的稿(design/11 §5.2 维度 3 的核心口径)。
        new_key = _group_subject(mats, new) or new_subj
        if _has_draft_for(new_subj, drafts) or _has_draft_for(new_key, drafts):
            continue                      # 今年已有稿 → 不缺
        old_subj = old.get("subject") or ""
        if not _had_draft_last_year(old_subj, drafts):
            continue                      # 往届也没稿 → 属维度 1, 不是"系列缺一篇"
        grp = _group_containing(mats, new)
        out.append(_topic("no_current", new_subj,
                          f"「{_series_key(old_subj)}」往届有稿({old_subj}), "
                          f"今年({new_subj})还没有稿件",
                          len(grp), authority_mean(grp or [new]),
                          eids=[m.get("eid") for m in (grp or [new])]))

    return rank_topics(out, top=0)


def _is_current_topic(topic: str, mats: List[dict],
                      window: Optional[dict]) -> bool:
    """稿件主题是否属**当届**活动(往届稿件的"素材薄"不构成当期选题)。

    口径: 主题与某个"当届素材组"同源 → 当届。窗口缺失时一律视为当届(不误杀)。
    """
    cur = str((window or {}).get("current") or "")
    if not cur:
        return True
    cur_year = cur[:4]
    for g in _group_by_series(mats):
        if str(g.get("year") or "") and str(g.get("year")) != cur_year:
            continue
        sk = _series_key(g.get("subject") or "")
        if sk and (sk in _series_key(topic) or _series_key(topic) in sk):
            return True
        if subject_similarity(topic, g.get("subject") or "") >= 0.8:
            return True
    return False


def _order_by_year(a: dict, b: dict) -> tuple:
    """按 period 年份定 (往届, 今年); 年份无法判定时返回 (None, None)。"""
    ya, yb = _year_of(a), _year_of(b)
    if not ya or not yb or ya == yb:
        return None, None
    return (a, b) if ya < yb else (b, a)


def _year_of(m: dict) -> str:
    p = m.get("period")
    start = str(p.get("start") or "") if isinstance(p, dict) else ""
    if not start:
        pp = Period.parse(m.get("content") or "")
        start = pp.start if pp else ""
    if len(start) >= 4 and start[:4].isdigit():
        return start[:4]
    m2 = _RE_YEAR.search(m.get("content") or "")
    return m2.group(1) if m2 else ""


def _group_containing(mats: List[dict], target: dict) -> List[dict]:
    for g in _group_by_series(mats):
        if any(m.get("eid") == target.get("eid") for m in g["materials"]):
            return g["materials"]
    return [target]


def _group_subject(mats: List[dict], target: dict) -> str:
    """目标素材所在**届次组**的名字(含年份前缀, 用于区分往届与今年)。"""
    for g in _group_by_series(mats):
        if any(m.get("eid") == target.get("eid") for m in g["materials"]):
            return g.get("subject") or ""
    return ""


def _topic(kind: str, subject: str, reason: str, gap_count: int,
           auth_mean: float, eids: Optional[List[str]] = None) -> dict:
    return {
        "kind": kind,
        "label": TOPIC_LABELS.get(kind, kind),
        "subject": subject,
        "reason": reason,
        "gap_count": int(gap_count),
        "authority_mean": round(float(auth_mean or 0.0), 4),
        "weight": TOPIC_WEIGHTS.get(kind, 0.5),
        "score": round(int(gap_count) * TOPIC_WEIGHTS.get(kind, 0.5)
                       * float(auth_mean or 0.0), 4),
        "eids": [e for e in (eids or []) if e],
    }


def rank_topics(topics: List[dict], top: int = TOP_N) -> List[dict]:
    """排序公式: 缺口数 × 类型权重 × 素材权威度均值(design/11 §5.2)。"""
    ranked = sorted(topics, key=lambda t: (-float(t.get("score") or 0.0),
                                           t.get("subject") or ""))
    return ranked[:top] if top else ranked


def _group_by_series(materials: List[dict]) -> List[dict]:
    """按"这场活动的这一届"给素材分组(design/11 §5.2 三维度扫描的基础)。

    三轮, 全确定性:
    1. `_group_by_activity`: 按**正文里的活动名**归组(素材 subject 只是标签, 不可靠);
    2. `_split_by_year`: 组内按 period 年份切分 —— "上届"与"本届"素材都提到同一个
       活动名, 但它们不是同一届, 分开才能看出"往届有稿、今年没稿";
    3. `_merge_unnamed`: 没有活动名的零散素材(现场照片说明、报名截图)按"同届 + 同类型"
       并回当届活动组 —— 它们本来就是这场活动的材料, 只是没重复活动名。
    """
    mats = [m for m in materials if (m.get("subject") or "").strip() or m.get("content")]
    groups: List[dict] = []
    for g in _group_by_activity(mats):
        groups.extend(_split_by_year(g))
    return _merge_unnamed(groups)


def _merge_unnamed(groups: List[dict]) -> List[dict]:
    """把"没有活动名"的零散素材并回同届的活动组(只并活动/通知/成果类素材)。"""
    named = [g for g in groups if g.get("activity")]
    unnamed = [g for g in groups if not g.get("activity")]
    for g in unnamed:
        merged = False
        for target in named:
            if target.get("year") != g.get("year"):
                continue
            if all((m.get("type") or "") in EVENT_MATERIAL_TYPES
                   for m in g["materials"]):
                target["materials"].extend(g["materials"])
                merged = True
                break
        if not merged:
            named.append(g)
    return [g for g in named if g["materials"]]


def _group_by_activity(mats: List[dict]) -> List[dict]:
    """按正文活动名归组; 没有活动名的按 subject 相似度归组。

    先按"活动名的具体程度"从长到短处理: 长活动名先建组, 短的(编程比赛)才可能并进去。
    只有一个活动名是另一个的子串时, 用更长的那个当组长("比赛" ← "编程马拉松比赛")。
    """
    ordered = sorted(mats, key=lambda m: -max(
        [len(n) for n in extract_activity_names(m.get("content") or "")] or [0]))
    groups: List[dict] = []
    for m in ordered:
        names = extract_activity_names(m.get("content") or "")
        subj = (m.get("subject") or "").strip()
        hit = None
        for g in groups:
            if g.get("activity") and (g["activity"] in names
                                      or any(n and (n in g["activity"]
                                                    or g["activity"] in n)
                                             for n in names)):
                hit = g
                break
        if hit is not None:
            for n in names:                    # 用更长的名字当组长
                if len(n) > len(hit["activity"]):
                    hit["activity"] = n
                    hit["key"] = _series_key(n)
                    hit["subject"] = n
            hit["materials"].append(m)
            continue
        if any(g.get("activity") for g in groups):
            pass
        elif subj:
            for g in groups:
                if not g.get("activity") and subject_similarity(g["subject"], subj) >= 0.9:
                    g["materials"].append(m)
                    hit = g
                    break
            if hit is not None:
                continue
        if names:
            act = max(names, key=len)
            groups.append({"key": _series_key(act), "subject": act,
                           "activity": act, "materials": [m]})
        else:
            key = _series_key(subj) or norm_text(m.get("content") or "")[:12]
            groups.append({"key": key, "subject": subj or key,
                           "activity": "", "materials": [m]})
    return groups


def _split_by_year(group: dict) -> List[dict]:
    """把一组素材按 period 年份拆成"届"分组(没有年份的归入最新那一届)。"""
    years = {_year_of(m) for m in group["materials"]}
    years.discard("")
    if len(years) < 2:
        g = dict(group)
        g["year"] = max(years) if years else ""
        return [g]
    latest = max(years)
    buckets: Dict[str, List[dict]] = {y: [] for y in years}
    for m in group["materials"]:
        y = _year_of(m) or latest
        buckets.setdefault(y, []).append(m)
    out = []
    for y in sorted(buckets, reverse=True):
        if not buckets[y]:
            continue
        subj = group["subject"]
        if y != latest:
            subj = f"{y}年{_series_key(group['subject'])}"
        out.append({"key": _series_key(subj), "subject": subj,
                    "materials": buckets[y], "year": y})
    return out


def _belongs_to_main(main: dict, g: dict, main_years: set) -> bool:
    """小组是否其实属于主活动: 同类型 + 同时间窗(往届素材因年份不同会被留下)。"""
    if not main_years:
        return False
    for m in g["materials"]:
        y = _year_of(m)
        if not y or y not in main_years:
            return False
        if (m.get("type") or "") not in EVENT_MATERIAL_TYPES:
            return False
    return True


def _same_edition(m: dict, base: str) -> bool:
    """"上届"的素材不能因为含"编程马拉松比赛"就被并进"本届"组。

    届次词(本届/上届/今年/去年/第N届)不一致 → 不是同一届, 必须分组, 否则
    "往届有今年无"就永远测不出来(design/11 §5.2 维度 3 的物质基础)。
    """
    ms, bs = _stage_word(m.get("subject") or ""), _stage_word(base)
    mc, bc = _stage_word(m.get("content") or ""), _stage_word(base)
    if ms and bs:
        return ms == bs
    if mc and bc:
        return mc == bc
    if ms and bc:
        return ms == bc
    if mc and bs:
        return mc == bs
    return True


def _stage_word(text: str) -> str:
    """取届次词(第一个命中的); 没有则空串。"""
    for w in ("本届", "上届", "今年", "去年"):
        if w in (text or ""):
            return w
    m = re.search(r"第[一二三四五六七八九十0-9]{1,3}届", text or "")
    return m.group(0) if m else ""


def _base_subject(mats: List[dict]) -> str:
    """全库最主要的"活动名": 从**正文**里抽活动名, 取出现最多者(零 LLM 的频率口径)。

    为什么不用 subject: 素材的 subject 是标签("签到表摘录"), 不同素材的标签天差地别,
    而活动名在正文里反复出现。届次("上届/本届")由 period 年份负责, 不混进活动名。
    """
    from collections import Counter
    names: List[str] = []
    for m in mats:
        names.extend(extract_activity_names(m.get("content") or ""))
    if names:
        return Counter(names).most_common(1)[0][0]
    subs = [m.get("subject") for m in mats if m.get("subject")]
    return Counter(subs).most_common(1)[0][0] if subs else ""


_ACT_RE = None
# 活动名左侧的"噪音字": 匹配到的名字里出现这些字, 说明把动词/介词/日期吞进来了。
# 中文没有词边界, 只能按这些字从右往左裁(取**最后一个**噪音字之后的部分)。
_ACT_NOISE = "在的了和与及由从对为把被是有这那于将日年月次名届件"\
             "选手参加举办承办协办组织负责共约"
_ACT_SUFFIX = ("比赛", "竞赛", "大赛", "活动", "讲座", "沙龙", "分享会", "培训", "晚会",
               "路演", "展会", "开放日", "纳新", "招新", "答辩", "汇报会", "表彰会",
               "见面会")
# 过于笼统的活动名: 单独出现时不足以代表一场活动("这次比赛" ≠ 某个活动名)
_GENERIC_ACTS = ("比赛", "竞赛", "大赛", "活动", "讲座", "晚会", "沙龙", "培训")
_ACT_NAME_RE = re.compile(
    r"[\u4e00-\u9fffA-Za-z0-9]{2,16}(?:" + "|".join(_ACT_SUFFIX) + r")")


def extract_activity_names(text: str) -> List[str]:
    """从正文抽活动名(内容域的核心名词, 零 LLM)。

    只抽**核心词**(以"比赛/讲座/晚会…"结尾), 不带"本届/上届"前缀 —— 届次由 period
    年份负责(`_split_by_year`), 两件事分开判定才不会互相污染。
    中文没有词边界, 所以按噪音字(在/的/举办/选手/日/届…)从右往左裁掉多余前缀;
    裁完只剩"比赛""活动"这种笼统词的不要 —— 它们代表不了一场活动。
    """
    raw = text or ""
    if not raw:
        return []
    out: List[str] = []
    for m in _ACT_NAME_RE.finditer(raw):
        core = _trim_activity(m.group(0))
        if core and core not in _GENERIC_ACTS and core in raw and core not in out:
            out.append(core)
    return out


def _trim_activity(name: str) -> str:
    """裁掉活动名左侧的噪音前缀("在大学生活动" → "大学生活动")。"""
    n = (name or "").strip()
    cut = 0
    for i, ch in enumerate(n):
        if ch in _ACT_NOISE:
            cut = i + 1
    n = n[cut:]
    if not (2 <= len(n) <= 12):
        return ""
    if any(w in n for w in ("举办", "承办", "协办", "组织", "负责", "选手", "参加")):
        return ""
    return n


def _stages(materials: List[dict]) -> List[dict]:
    """"届次"视图: 每条素材的 period 年份 + 是否往届(design/11 §5.2 维度 3)。

    往届判定用确定性口径: period 年份小于组内最大年份。没有 period 的素材不进届次视图。
    """
    rows = []
    for m in materials:
        p = (Period.from_dict(m.get("period")) if m.get("period")
             else Period.parse(m.get("content") or ""))
        if p is None or not p.start:
            continue
        rows.append({"eid": m.get("eid"), "year": str(p.start)[:4], "start": p.start})
    if not rows:
        return []
    latest = max(r["year"] for r in rows)
    for r in rows:
        r["late"] = r["year"] < latest
    return rows


def _had_draft_last_year(subject: str, drafts: List[dict]) -> bool:
    """往届是否有稿: 稿件主题与该系列活动名同源即算(历史层稿件)."""
    sk = _series_key(subject)
    if not sk:
        return False
    for d in drafts:
        dt = _series_key(d.get("topic") or "")
        if dt and (dt == sk or sk in dt or dt in sk):
            return True
    return False


def _cited_eids(topic: str, cites: Dict[str, dict], draft: dict) -> List[str]:
    """该稿件主题相关的 cites 边指向的素材 eid 集合。"""
    out: List[str] = []
    for edge in (cites or {}).values():
        tgt = edge.get("tgt")
        if tgt and tgt not in out:
            out.append(tgt)
    declared = draft.get("eids") or []
    for e in declared:
        if e and e not in out:
            out.append(e)
    return out


def _same_series(text: str, topic: str) -> bool:
    if not text or not topic:
        return False
    sk = _series_key(topic)
    return bool(sk) and (sk in norm_text(text) or subject_similarity(topic, text) >= 0.6)


__all__ = [
    "TOPIC_WEIGHTS", "TOPIC_LABELS", "MIN_ACTIVITY_MATERIALS", "MIN_CITES",
    "MIN_NO_SOURCE", "TOP_N", "scan_topics", "rank_topics",
]
