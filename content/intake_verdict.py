# coding: utf-8
"""content/intake_verdict.py — 素材录入的切句与"给人看的一句话裁决"。

`analyze_material_text()` 产出的是**规则级**裁决(带规则编号、置信度、候选 eid),
而界面上要的是"这句话跟库里什么关系 + 一句话原因"。这里就是这两件事:

  - `split_sentences()`: 按句末标点/换行切句(与前端 doc-board.js 同一口径);
  - `simplify_verdict()`: 规则判决 → {kind, reason, evidence[], target_eid}。

这三件事原先散在别处的记忆服务里, 交付版收进内容域: 切句/抽主体/一句话裁决
都属于"素材入库"这一步, 不该为了切个句去依赖一套更重的服务。
"""
from __future__ import annotations

import re
from typing import Dict, List

from graphstore.types import norm_text

_SENT_SPLIT_RE = re.compile(r"[。！？!?\n]+")

# 抽主体时要剥掉的开头虚词(否则"我们举办的活动"会把"我们"当主体)
_LEAD_STOP = ("我", "我们", "本人", "然后", "当时", "期间", "另外", "那年",
              "去年", "今年", "主要", "还", "也", "在", "了")

# 规则编号 → 一句话原因(给用户看的, 不出现规则代号)
RULE_REASON = {
    "R1": "与已有记录内容高度重复",
    "R2": "与已有记录的时间不一致",
    "R3": "与已有记录的量化数字不一致",
    "R4": "技术栈与已有记录不一致",
    "R6": "可作为已有记录的补充细节",
    "SIM": "与已有记录语义相近",
}

# 裁决类型 → 一句话原因(前端徽章上的 kind 与它对应)
KIND_REASON = {
    "conflict": "与已有记录存在矛盾",
    "isolated": "与已有内容相关, 但暂无法可靠归属",
    "detail_of": "是已有记录的补充细节",
    "new": "库里暂无相关记录",
    "low_confidence": "机器无法可靠判断归属, 需要你决定",
}


def split_sentences(text: str) -> List[str]:
    """按句末标点/换行切句。"""
    out = []
    for seg in _SENT_SPLIT_RE.split(text or ""):
        seg = seg.strip()
        if seg:
            out.append(seg)
    return out


def guess_subject(text: str, etype: str, techs: List[str]) -> str:
    """素材主体启发式抽取(规则词典; 拿不准时由用户裁决修正, 不猜第二次)。"""
    t = (text or "").strip()
    if etype == "skill" and techs:
        return techs[0]
    if etype == "competition":
        m = re.search(r"([\u4e00-\u9fffA-Za-z0-9·]{2,24}?(?:竞赛|比赛|大赛))", t)
        if m:
            return m.group(1)
    if etype == "education":
        m = re.search(r"([\u4e00-\u9fffA-Za-z·]{2,24}?(?:大学|学院|学校))", t)
        if m:
            return m.group(1)
    if etype == "achievement":
        m = re.search(r"([\u4e00-\u9fffA-Za-z0-9]{2,30}?(?:论文|专利))", t)
        if m:
            return m.group(1)
    m = re.search(r"([\u4e00-\u9fffA-Za-z0-9]{2,16}?(?:系统|平台|网站|应用|项目|小程序|模型|算法|工具))", t)
    if m:
        return m.group(1)
    core = norm_text(t)
    for w in _LEAD_STOP:
        core = core.replace(w, " ", 1) if core.startswith(w) else core
    core = " ".join(core.split())
    return core[:10] or "未命名素材"


def simplify_verdict(verdict: dict, events: Dict[str, dict]) -> dict:
    """给 UI 一条"最直接依据素材 + 一句话原因", 去掉规则噪音字段。

    展示不含置信度等数字(证据 = 一句话 + 一条已有素材)。
    """
    kind = (verdict or {}).get("kind", "low_confidence")
    reason = KIND_REASON.get(kind, "")
    evidence: List[dict] = []
    for ev in (verdict or {}).get("evidence") or []:
        rule = ev.get("rule", "")
        older_eid = ev.get("older_eid")
        older = events.get(older_eid) if older_eid else None
        item = {"reason": RULE_REASON.get(rule, reason or "与已有记录相关")}
        if older:
            item["event"] = {
                "subject": older.get("subject", ""),
                "content": (older.get("content") or "")[:60],
            }
        evidence.append(item)
    if not evidence and (verdict or {}).get("target_eid"):
        older = events.get(verdict["target_eid"])
        if older:
            evidence.append({
                "reason": reason,
                "event": {"subject": older.get("subject", ""),
                          "content": (older.get("content") or "")[:60]},
            })
    return {
        "kind": kind,
        "reason": reason,
        "evidence": evidence,
        "target_eid": (verdict or {}).get("target_eid"),
    }


__all__ = ["split_sentences", "guess_subject", "simplify_verdict",
           "RULE_REASON", "KIND_REASON"]
