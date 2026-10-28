# coding: utf-8
"""graphstore/rules.py — 事件图的确定性规则推导(纯函数, 零 LLM/零 embed)。

时序边规则(design/07 §4.1 + design/11 §4.2): 同主体事件的 period 区间推导
before/overlap/contains。

分组口径有两级(内容域新增第二级):
1. **subject 相同**(原口径): 素材与稿件都适用;
2. **活动名相同**(design/11 §5.2 维度 3 的物质基础): 内容域素材的 subject 是标签
   ("签到表摘录""现场照片说明二"), 活动名只在正文里, 光靠 subject 永远建不出
   "往届 ↔ 今年"的边, 于是"往届有今年无"这类选题判定就失去依据。

- period 为 "YYYY" / "YYYY-MM" / "MM-DD" 字符串, 字典序比较即时间序;
- end=None 视为开放区间(至今), 不参与 before 推断;
- **跨年份的一对一定判为 before**(2024 的上届 vs 2025 的今年, 这才是"往届");
- 结果 key 与 file_graph 的有向图约定一致: "{src}>{tgt}"。
"""
from __future__ import annotations

import re
from typing import Dict, List

from .types import Period

# 活动名后缀词表(与 content/topic_gap.py 同口径; 这里独立一份, 让 graphstore 不依赖 content)
_ACT_SUFFIX = ("比赛", "竞赛", "大赛", "活动", "讲座", "沙龙", "分享会", "培训", "晚会",
               "路演", "展会", "开放日", "纳新", "招新", "答辩", "汇报会", "表彰会",
               "见面会")
_ACT_NAME_RE = re.compile(
    r"[\u4e00-\u9fffA-Za-z0-9]{2,16}(?:" + "|".join(_ACT_SUFFIX) + r")")
# 噪音字: 中文没有词边界, 匹配到的名字里出现这些字说明吞了动词/介词/日期, 从右往左裁
_ACT_NOISE = "在的了和与及由从对为把被是有这那于将日年月次名届件选手参加举办承办协办组织负责共约"


def activity_names(text: str) -> List[str]:
    """正文里的活动名(确定性抽取; 与 content.topic_gap.extract_activity_names 同口径)。"""
    raw = text or ""
    if not raw:
        return []
    out: List[str] = []
    for m in _ACT_NAME_RE.finditer(raw):
        core = _trim_activity(m.group(0))
        if core and core in raw and core not in out:
            out.append(core)
    return out


def _trim_activity(name: str) -> str:
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


def _period_pair(ev) -> tuple:
    """(start, end) 或 None; end=None 表示开放。"""
    p = Period.from_dict(ev.get("period")) if ev.get("period") else None
    if p is None or not p.start:
        return None
    return (p.start, p.end)


def _keys_of(ev: dict) -> List[str]:
    """一条素材的"同一活动"键: subject + 正文抽到的活动名。"""
    keys = []
    subj = (ev.get("subject") or "").strip()
    if subj:
        keys.append(subj)
    for name in activity_names(ev.get("content") or ""):
        if name not in keys:
            keys.append(name)
    return keys


def _is_cross_year(ae, as_, be, bs) -> bool:
    """两端年份不同("2024-11" vs "2025-11" / "2024" vs "2025")→ 往届 vs 今年。"""
    ya, yb = str(as_)[:4], str(bs)[:4]
    return len(ya) == 4 and len(yb) == 4 and ya.isdigit() and yb.isdigit() and ya != yb


def derive_rule_temporal_edges(events: Dict[str, dict]) -> Dict[str, dict]:
    """同一活动的事件对 → 时序边(纯函数)。

    返回 {f"{src}>{tgt}": {src,tgt,kind,weight,evidence,source:"rule"}}。
    区间比较(字符串): a.end < b.start → before; 互相包含 → contains; 相交 → overlap。
    """
    by_key: Dict[str, List[str]] = {}
    for eid, ev in events.items():
        for key in _keys_of(ev):
            bucket = by_key.setdefault(key, [])
            if eid not in bucket:
                bucket.append(eid)

    edges: Dict[str, dict] = {}
    for eids in by_key.values():
        pairs = []
        for eid in eids:
            pp = _period_pair(events[eid])
            if pp is not None:
                pairs.append((eid, pp))
        for i in range(len(pairs)):
            for j in range(i + 1, len(pairs)):
                a, (as_, ae) = pairs[i]
                b, (bs, be) = pairs[j]
                if f"{a}>{b}" in edges or f"{b}>{a}" in edges:
                    continue                       # 同一对只建一条边
                a_raw = (events[a].get("period") or {}).get("raw", "")
                b_raw = (events[b].get("period") or {}).get("raw", "")
                kind, weight, src, tgt = None, None, None, None
                if ae is not None and ae < bs:
                    kind, weight, src, tgt = "before", 1.0, a, b
                elif be is not None and be < as_:
                    kind, weight, src, tgt = "before", 1.0, b, a
                elif _is_cross_year(ae, as_, be, bs):
                    # 跨届: 早的那年是"往届", 晚的那年是"今年"(更早 → 更晚)
                    src, tgt = (a, b) if as_ < bs else (b, a)
                    kind, weight = "before", 1.0
                elif ae is not None and bs <= as_ and be <= ae:
                    kind, weight, src, tgt = "contains", 0.8, a, b
                elif be is not None and as_ <= bs and ae <= be:
                    kind, weight, src, tgt = "contains", 0.8, b, a
                elif as_ < bs or (as_ == bs and (ae or "") != (be or "")):
                    kind, weight, src, tgt = "overlap", 0.7, a, b
                else:
                    continue
                edges[f"{src}>{tgt}"] = {
                    "src": src, "tgt": tgt, "kind": kind, "weight": weight,
                    "evidence": f"{a_raw} vs {b_raw}", "source": "rule",
                }
    return edges
