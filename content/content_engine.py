# coding: utf-8
"""content/content_engine.py — 成稿引擎: 决策链 + 段状态机 + 收敛(design/11 §5.3)。

结构沿用原 `interview_engine.py` 的**决策链**, 候选源整体换到内容域:

| 原候选源 | 新候选源 | 本模块函数 |
|---|---|---|
| 1. 薄弱点追问 `_weak_followup` | **缺口补段** `_gap_segment` | `_gap_segment` |
| 2. 弱模板兜底 | **缺口模板兜底** | `_gap_template` |
| 3. 过短展开追问 | **素材不足拆分** | `_split_segment` |
| 4. 意图抽题 | **文体意图抽段** | `_intent_draw` |
| 5. 题库随机 `pop_random` | **选题库轮询** `TopicStore.pop_next` | `_topic_rotate` |
| 6. LLM 单题 (P2) | **LLM 单段 (P2)** | `_llm_segment` |
| 7. 模板兜底 | **模板兜底** | `_template_segment` |

保留的三样东西(design/11 §3.3): 决策链结构本身、7 级降级顺序、去重守卫与收敛判定。
段状态机: `pending → drafted → verified | blocked`(blocked = 核验有阻断项)。

常量旋钮数值全部保留、语义平移(见文件末尾的 MIN_CLAUSE_CHARS / MIN_SEGMENT_MATERIALS)。
"""
from __future__ import annotations

import logging
import os
import re
from typing import Dict, List, Optional

from graphstore.types import NO_SOURCE, Period, norm_text, subject_similarity

from content import platform_spec as spec
from content.fact_verifier import activity_window, top_weak_sorted
from content.material_ops import segment_source_refs

logger = logging.getLogger(__name__)

# 一段至少要有几条素材支撑(内容域旋钮, env 可覆盖; 低于此值走缺口补段)
MIN_SEGMENT_MATERIALS = int(os.getenv("MIN_SEGMENT_MATERIALS", "2"))

# 段结构(与 platform_spec.SEGMENTS 同一口径)
SEGMENTS = spec.SEGMENTS
SEGMENT_LABELS = spec.SEGMENT_LABELS

# 段状态机
S_SEG_PENDING = "pending"
S_SEG_DRAFTED = "drafted"
S_SEG_VERIFIED = "verified"
S_SEG_BLOCKED = "blocked"

# 段 → 该段主要吃哪类素材(决定"素材不足"时怎么拆)
SEGMENT_MATERIAL_TYPES = {
    "lead": ("notice", "activity"),
    "background": ("background", "org", "notice"),
    "process": ("activity", "person", "notice"),
    "highlight": ("activity", "notice", "achievement"),
    "impact": ("achievement", "person", "activity"),
    "closing": ("notice", "org", "person"),
}

# 兜底模板(零 LLM; 只把素材事实填进句子, 不自己加形容词)
TEMPLATES = {
    "lead": "{subject}{verb}{period}{venue}。",
    "background": "{org}每年举办{subject}。",
    "process": "{subject}; {facts}。",
    "highlight": "{facts}。",
    "impact": "{facts}。",
    "closing": "{subject}由{org}组织, 参与者可向{role}咨询。",
}
_VERB = {"activity": "举办", "notice": "发布", "achievement": "获得", "person": "参与",
         "org": "开展", "background": "记录"}


def _fact_phrase(ev: dict) -> str:
    """把素材压成一句可用的事实短语(不改写、不加评价)。"""
    content = (ev.get("content") or "").strip()
    for sep in ("：", ":"):
        if sep in content:
            content = content.split(sep, 1)[1].strip() or content
    content = content.strip("。 ;；")
    return content


class ContentEngine:
    """成稿大脑: 输入素材图 + 缺口, 输出段结构稿(每段带 source_refs)。"""

    def __init__(self):
        self._last_seg: Dict[str, str] = {}       # 去重守卫: 学生 → 上一次的段落文本
        self._intent_i = 0

    # ---------- 入口 ----------
    def draft(self, materials: List[dict], topics: Optional[List[dict]] = None,
              gap_result: Optional[dict] = None, intents: Optional[List[str]] = None,
              window: Optional[dict] = None, max_segments: int = 6,
              use_llm: bool = False) -> dict:
        """生成一稿: 决策链逐段产出, 直到段数达上限或缺口全被覆盖(收敛)。

        materials: 素材列表(events_payload 形状, 带 eid/authority/period)
        gap_result: 最近一次素材级核验结果(可空, 空则先做一次缺口检测)
        intents: 文体意图候选(通知类/预告/报道/采访/喜报), 用于"意图抽段"
        返回 {topic, segments, gaps, cite_edges, converged, steps, window, block}
        """
        mats = _order_materials(materials or [])
        window = window or activity_window(mats)
        topic = _pick_topic(mats, topics)

        # 没有缺口信息就先扫一遍(决策链第 1 步靠它)
        if gap_result is None:
            from content.fact_verifier import detect_conflicts, detect_sensitive, is_isolated, is_stale
            gaps = _material_level_gaps(mats, window, detect_conflicts, detect_sensitive,
                                        is_isolated, is_stale)
        else:
            gaps = list(gap_result.get("gaps") or [])
        open_gaps = top_weak_sorted([g for g in gaps if not g.get("resolved")], top=0)

        segments: List[dict] = []
        steps: List[dict] = []
        used: set = set()          # 已取用的分句 (eid, clause_index) — 去重守卫
        covered: set = set()       # 已被段落覆盖的缺口 claim_key

        while len(segments) < max(1, int(max_segments)):
            want = SEGMENTS[len(segments)]
            seg, step = self._next_segment(
                mats, want, topic, open_gaps, covered, used,
                intents or [], len(segments), use_llm)
            if seg is None:
                break
            segments.append(seg)
            steps.append({"index": len(segments), "segment": want,
                          "source_used": step.get("source"),
                          "reason": step.get("reason", ""),
                          "gap_key": step.get("gap_key", "")})
            if step.get("gap_key"):
                covered.add(step["gap_key"])
            covered |= set(seg.get("covered_claims") or ())
            # 收敛: 素材已取尽, 且缺口全被覆盖 → 停(不硬凑段数)
            if step.get("source") == "template_empty" and \
                    all(_gap_covered(g, covered) for g in open_gaps):
                break

        cite_edges = _cite_edges(segments, mats)
        uncovered = [g for g in open_gaps if not _gap_covered(g, covered)]
        return {
            "topic": topic,
            "segments": segments,
            "gaps": open_gaps,
            "uncovered": uncovered,
            "cite_edges": cite_edges,
            "converged": {
                "segments": len(segments),
                "cap": max_segments,
                "by_cap": len(segments) >= max_segments,
                "gaps_total": len(open_gaps),
                "gaps_covered": len(open_gaps) - len(uncovered),
                "gaps_uncovered": len(uncovered),
                "by_gap_cover": bool(open_gaps) and not uncovered,
            },
            "steps": steps,
            "window": window,
        }

    # ---------- 决策链(7 级, 顺序即优先级) ----------
    def _next_segment(self, mats: List[dict], want: str, topic: str,
                      open_gaps: List[dict], covered: set, used: set,
                      intents: List[str], index: int, use_llm: bool):
        """为一个段位选料并成段(七级降级, 顺序即优先级):

        ① 缺口补段 → ② 缺口模板兜底 → ③ 按段位类型取分句 →
        ④ 素材不足拆分 → ⑤ 文体意图抽段 → ⑥ 选题库轮询 → ⑦ (LLM 单段) → ⑧ 模板兜底。

        素材整体不足时(所有分句都被取过)会一路降到模板兜底, 这正是 design/11 §1 的
        "宁可功能少一半也要端到端能跑两遍不翻车": 没有 LLM、没有剩余素材也照样出稿。
        """
        # 1. 缺口补段
        r = self._gap_segment(mats, want, open_gaps, covered, index, used)
        if r is not None:
            return r
        # 2. 缺口模板兜底(缺口还在, 但没有匹配该段的素材 → 用缺口原句成段)
        r = self._gap_template(mats, want, open_gaps, covered, index)
        if r is not None:
            return r
        # 3. 按该段素材类型取一个分句(内容域的正常取料路径)
        r = self._split_segment(mats, want, used, index)
        if r is not None:
            return r
        # 4. 素材不足拆分
        r = self._thin_segment(mats, want, used, index)
        if r is not None:
            return r
        # 5. 文体意图抽段
        r = self._intent_draw(mats, want, intents, index, used)
        if r is not None:
            return r
        # 6. 选题库轮询(本引擎只按主题给候选, 真正轮询计数在 TopicStore)
        r = self._topic_rotate(mats, want, topic, index, used)
        if r is not None:
            return r
        # 7. LLM 单段(P2; 需调用方显式授权/闸门放行, 本引擎只标注候选)
        if use_llm:
            r = self._llm_segment(mats, want, topic, index, used)
            if r is not None:
                return r
        # 8. 模板兜底
        return self._template_segment(mats, want, topic, index, used)

    def _gap_segment(self, mats: List[dict], want: str, open_gaps: List[dict],
                     covered: set, index: int, used: set):
        """按 top-3 缺口补对应段落(缺口 → 段位的确定性映射)。

        缺口类型决定段位: 无据 → 亮点/影响(常出数字); 矛盾 → 导语/亮点(并列两版);
        过期 → 背景(往届信息); 孤证 → 过程(需补权威来源); 涉隐私 → 结语(处置说明)。
        """
        want_types = _GAP_SEGMENT_MAP.get(want, ())
        for g in open_gaps:
            if not _is_open(g, covered) or g.get("weakness_type") not in want_types:
                continue
            # 涉隐私的缺口**绝对不能**把原文写进稿件: 缺口补段会照抄素材正文, 正好会把
            # 手机号/住址抄进稿子。这类缺口交给下一级"缺口模板兜底"处理成一句处置说明。
            if g.get("weakness_type") == SENSITIVE_GAP:
                continue
            eid = g.get("source_eid")
            ev = _by_eid(mats, eid) if eid else None
            if ev is None:
                continue
            got = _take_any_clause(ev, used, avoid_sensitive=False)
            if got is None:
                continue
            seg = _make_segment(want, got["text"], [ev.get("eid")], index)
            return seg, {"source": "gap_segment", "gap_key": g.get("claim_key"),
                         "reason": f"缺口[{g.get('weakness_type')}] {g.get('assertion')}"}
        return None

    def _gap_template(self, mats: List[dict], want: str, open_gaps: List[dict],
                      covered: set, index: int):
        """缺口模板兜底: 缺口指向的素材已被别段用掉时, 直接按缺口原句成模板段。

        这里**只复述缺口本身**(如"到场人数 200 人 / 183 人 说法不一"), 不替用户选一个,
        这正是"不替用户造事实"的体现(design/11 §4.3: 矛盾并列展示由用户选)。
        """
        want_types = _GAP_SEGMENT_MAP.get(want, ())
        for g in open_gaps:
            if not _is_open(g, covered) or g.get("weakness_type") not in want_types:
                continue
            assertion = g.get("assertion") or ""
            if not assertion:
                continue
            if g.get("weakness_type") == "conflict":
                text = f"关于{assertion}, 素材中说法不一, 待主办方确认后再定稿。"
                eids = [c.get("eid") for c in (g.get("candidates") or [])[:2]
                        if c.get("eid")]
            elif g.get("weakness_type") == SENSITIVE_GAP:
                # 涉隐私: 只说"需脱敏后发布", 不复述那条隐私内容(稿件里不许出现)
                text = ("部分素材含个人信息, 发布前需脱敏或替换为联系方式占位说明。")
                eids = []
            elif g.get("weakness_type") == STALE_LOCAL:
                text = "往届情况(勿直接引用): " + (g.get("claim") or "")[:60]
                eids = [g.get("source_eid")] if g.get("source_eid") else []
            else:
                text = "待核实: " + (g.get("claim") or assertion)[:80]
                eids = [g.get("source_eid")] if g.get("source_eid") else []
            # 缺口兜底段不登记 source_refs: 它引用的素材尚未被确认可用,
            # 登记了会污染溯源链(前端会显示"这句话有来源", 而实际是待核实)
            seg = _make_segment(want, text, [], index, gap_only=True)
            seg["gap_refs"] = [e for e in eids if e]
            return seg, {"source": "gap_template", "gap_key": g.get("claim_key"),
                         "reason": f"缺口模板兜底[{g.get('weakness_type')}]"}
        return None

    def _split_segment(self, mats: List[dict], want: str, used: set,
                       index: int):
        """按该段素材类型取**一个分句**作为该段正文(内容域的正常取料路径)。

        取分句而不是整条素材: 一条素材常含好几件事("预计200人参加, 队伍12支, 志愿者50人"),
        整条塞进一段会让这一段同时断言多件事, 缺口糊在一起、溯源链也指不准。
        """
        want_types = SEGMENT_MATERIAL_TYPES.get(want, ())
        got = pick_clause(mats, want_types, used, avoid_sensitive=True, lenient=False)
        if got is None:
            return None
        seg = _make_segment(want, got["text"], [got["eid"]], index,
                            authority=got.get("authority"))
        return seg, {"source": "type_pick",
                     "reason": f"按段位类型取分句(素材 {got.get('eid')} 第 "
                               f"{got.get('clause_index')} 句)"}

    def _thin_segment(self, mats: List[dict], want: str, used: set,
                      index: int):
        """素材不足拆分: 各类型都没分句可取时, 放宽判定(允许复用含隐私分句以外的任何句子)。

        走到这一级说明"素材确实不够", 所以段里**如实记下**缺哪类素材,
        而不是拿别的段落的素材硬凑(design/11 §5.3 第 3 级: 素材不足拆分)。
        """
        if len(mats) < MIN_SEGMENT_MATERIALS:
            return None
        got = pick_clause(mats, tuple(), used, avoid_sensitive=True)
        if got is None:
            return None
        seg = _make_segment(want, got["text"], [got["eid"]], index,
                            thin_source=True)
        return seg, {"source": "split_segment",
                     "reason": f"该段位类型素材不足, 放宽取分句({got.get('eid')})"}

    def _intent_draw(self, mats: List[dict], want: str, intents: List[str],
                     index: int, used: set):
        """文体意图抽段: 意图(通知类/预告/报道/采访/喜报)决定段的写法侧重。"""
        if not intents:
            return None
        intent = intents[index % len(intents)]
        want_types = SEGMENT_MATERIAL_TYPES.get(want, ())
        got = pick_clause(mats, want_types, used, avoid_sensitive=True)
        if got is None:
            return None
        seg = _make_segment(want, got["text"], [got["eid"]], index, intent=intent)
        return seg, {"source": "intent_draw", "reason": f"文体意图抽段: {intent}"}

    def _topic_rotate(self, mats: List[dict], want: str, topic: str, index: int,
                      used: set):
        """选题库轮询: 按选题主题取最相关素材的一个分句(轮询计数由 TopicStore 记账)。"""
        if not topic:
            return None
        scored = [(subject_similarity(topic, m.get("content") or ""), m) for m in mats]
        scored = [s for s in scored if s[0] > 0]
        if not scored:
            return None
        scored.sort(key=lambda x: -x[0])
        for _sim, ev in scored:
            got = _take_any_clause(ev, used, avoid_sensitive=True)
            if got is None:
                continue
            seg = _make_segment(want, got["text"], [ev.get("eid")], index, topic=topic)
            return seg, {"source": "topic_rotate", "reason": f"选题库轮询命中「{topic}」"}
        return None

    def _llm_segment(self, mats: List[dict], want: str, topic: str, index: int,
                     used: set):
        """LLM 单段(P2 候选): 只标注"这一段可交给 LLM 起草"。

        本引擎保持零 LLM: 要让 LLM 参与, 由 content_llm.generate_segments 在闸门
        放行后调用, 并用 LLM 结果**替换**这里的模板段(替换后仍要过断言级 diff)。
        """
        for ev in mats:
            got = _take_any_clause(ev, used, avoid_sensitive=True)
            if got is None:
                continue
            seg = _make_segment(want, got["text"], [ev.get("eid")], index,
                                llm_draft=True)
            return seg, {"source": "llm_segment", "reason": "LLM 单段候选(待闸门放行)"}
        return None

    def _template_segment(self, mats: List[dict], want: str, topic: str, index: int,
                          used: set):
        """模板兜底(最后一级): 轮转取素材主体出模板段; 素材为空则给空模板段。"""
        if not mats:
            seg = _make_segment(want, "", [], index, source="template_empty")
            return seg, {"source": "template_empty", "reason": "素材库为空, 模板占位"}
        got = pick_clause(mats, SEGMENT_MATERIAL_TYPES.get(want, ()), used,
                          avoid_sensitive=False)
        if got is not None:
            ev = _by_eid(mats, got["eid"]) or {}
            tpl = TEMPLATES.get(want, "{facts}。")
            text = tpl.format(subject=ev.get("subject") or topic or "本次活动",
                              verb=_VERB.get(ev.get("type") or "", "举办"),
                              period=_period_text(ev), venue=_venue_text(ev),
                              org=ev.get("org") or "主办方",
                              role=ev.get("role") or "负责人",
                              facts=got["text"])
            text = text.replace("。。", "。")
            seg = _make_segment(want, text, [got["eid"]], index, source="template")
            return seg, {"source": "template", "reason": "模板兜底(套模板填素材事实)"}
        seg = _make_segment(want, "", [], index, source="template_empty")
        return seg, {"source": "template_empty", "reason": "素材已取尽, 模板占位"}

    # ---------- 段状态推进 ----------
    @staticmethod
    def apply_verification(draft: dict, verify_result: dict) -> dict:
        """把核验结果写回段状态机: drafted → verified | blocked(design/11 §5.5 门禁)。

        - 段落有阻断项 → blocked(不许进入多平台适配);
        - 无阻断项 → verified(可发布, 但可能有警告级缺口)。
        """
        by_seg = {s.get("seg_id"): s for s in verify_result.get("segments") or []}
        for seg in draft.get("segments") or []:
            r = by_seg.get(seg.get("seg_id"))
            if r is None:
                continue
            seg["claims"] = [{k: v for k, v in c.items() if k != "supports"}
                             for c in r.get("claims") or []]
            seg["gaps"] = r.get("gaps") or []
            # 段级门禁: 该段**自己有**阻断项才算 blocked(别处的阻断项不该把这一段也标红)
            seg["status"] = (S_SEG_BLOCKED
                             if any(g.get("blocking") for g in seg["gaps"])
                             else S_SEG_VERIFIED)
        draft["block"] = bool(verify_result.get("block"))
        draft["verified"] = True
        return draft


# 缺口类型 → 段位映射(design/11 §5.3 决策链第 1 级)
STALE_LOCAL = "stale"
SENSITIVE_GAP = "sensitive"
_GAP_SEGMENT_MAP = {
    "lead": ("conflict",),
    "background": ("stale",),
    "process": ("isolated",),
    "highlight": ("conflict", "no_source"),
    "impact": ("no_source", "isolated"),
    "closing": ("sensitive",),
}


def _make_segment(want: str, text: str, refs: List[str], index: int = 0,
                  **extra) -> dict:
    """构造一个段落。seg_id 带序号: 同一稿里可能有两段同类型(亮点 1/亮点 2),
    而 cites 边键、核验结果、前端高亮都按 seg_id 对齐, 不能撞。"""
    seg = {
        "seg_id": f"seg_{index + 1}_{want}",
        "kind": want,
        "label": SEGMENT_LABELS.get(want, want),
        "text": (text or "").strip(),
        "source_refs": [r for r in refs if r],
        "status": S_SEG_DRAFTED,
    }
    seg.update(extra)
    return seg


def take_clause(material: dict, used: Optional[set] = None,
                want_subject: Optional[str] = None) -> Optional[dict]:
    """从一条素材里取**一个可用分句**(成稿原子单位)。

    素材正文常含多条信息("预计约200人参加, 报名队伍共12支, 志愿者共50人到场支持"),
    整条塞进一段会让这一段同时断言好几件事, 核验出来的缺口也糊在一起。
    分句后每段只断言一件事, 溯源链才能指到"哪句话来自哪条素材"。

    只收**能独立成句**的分句(短于 MIN_CLAUSE_CHARS 的残片、"2024年11月16日" 这种
    纯时间碎片一律跳过, 否则段里会出现一个孤零零的日期)。
    """
    from content.fact_verifier import detect_sensitive
    used = used if used is not None else set()
    content = (material.get("content") or "").strip()
    if not content:
        return None
    for sep in ("：", ":"):
        if sep in content:
            head, tail = content.split(sep, 1)
            tail = tail.strip()
            # "关于举办X的通知: 11月20日..." 这种冒号前只是标题, 冒号后才是正文;
            # 但冒号后若太短(或已无内容)就保留原句, 不要把人话切碎。
            if len(head) <= 20 and _usable_clause(tail):
                content = tail
            break
    clauses = [_clean_clause(c) for c in re.split(r"[，,；;。]", content)]
    clauses = [c for c in clauses if c]
    for i, c in enumerate(clauses):
        key = (material.get("eid"), i)
        if key in used:
            continue
        if not _usable_clause(c):
            used.add(key)                            # 残片直接作废, 不再重复试
            continue
        used.add(key)
        return {"text": c, "eid": material.get("eid"), "clause_index": i,
                "sensitive": bool(detect_sensitive(c)),
                "authority": float(material.get("authority") or 0.0),
                "type": material.get("type"), "source": material.get("source")}
    return None


# 段落正文的最小长度: 比这还短的通常是日期/数字残片, 不配单独成段
MIN_CLAUSE_CHARS = 8


def _clean_clause(text: str) -> str:
    """收尾清理: 去掉分句切分留下的悬空冒号/连接词残尾, 不改变任何事实。"""
    t = (text or "").strip()
    while t and t[-1] in "：:、 　":
        t = t[:-1].strip()
    return t


def _usable_clause(text: str) -> bool:
    """分句能否独立成句: 长度够 + 有中文实词(不是纯数字/纯日期)。"""
    t = (text or "").strip()
    if len(t) < MIN_CLAUSE_CHARS:
        return False
    cjk = sum(1 for ch in t if "\u4e00" <= ch <= "\u9fff")
    return cjk >= 4


def pick_clause(mats: List[dict], types: tuple, used: set,
                avoid_sensitive: bool = True,
                lenient: bool = True) -> Optional[dict]:
    """按素材类型顺序取一个分句: 类型匹配优先, 其次权威度高者, 再次未用过者。

    avoid_sensitive: 含隐私的分句默认跳过(它们本来就该被 sensitive 缺口挡住,
    不该被成稿引擎主动写进稿件里)。
    lenient=True 时类型都不匹配就放宽到全库取(决策链下一级也有同样的放宽,
    所以"按类型取料"这一级应当传 False, 由下一级显式承担放宽的语义)。
    """
    for t in types:
        cands = [m for m in mats if m.get("type") == t]
        cands.sort(key=lambda m: -float(m.get("authority") or 0.0))
        for m in cands:
            got = _take_any_clause(m, used, avoid_sensitive)
            if got is not None:
                return got
    if not lenient:
        return None
    for m in mats:                       # 类型不匹配时放宽到全库(仍按权威度)
        got = _take_any_clause(m, used, avoid_sensitive)
        if got is not None:
            return got
    return None


def _take_any_clause(m: dict, used: set, avoid_sensitive: bool) -> Optional[dict]:
    """在一条素材上反复取分句; 含隐私的分句直接跳过并作废(最多试 6 个分句)。

    跳过时必须把分句记进 used, 否则下一次调用会把同一个含隐私分句之外的内容
    又取一遍, 反而漏出"3号宿舍楼412室"这种本该被挡住的片段。
    """
    for _ in range(6):
        got = take_clause(m, used)
        if got is None:
            return None
        if avoid_sensitive and got.get("sensitive"):
            continue
        return got
    return None


def _by_eid(mats: List[dict], eid: Optional[str]) -> Optional[dict]:
    for m in mats:
        if m.get("eid") == eid:
            return m
    return None


def _period_text(ev: dict) -> str:
    p = ev.get("period")
    if isinstance(p, dict) and p.get("raw"):
        return p["raw"]
    return ""


def _venue_text(ev: dict) -> str:
    from content.material_ops import extract_entities
    for e in extract_entities(ev.get("content") or "", None):
        if e["kind"] == "venue":
            return "在" + e["text"]
    return ""


def _is_open(gap: dict, covered: set) -> bool:
    return gap.get("claim_key") not in covered and not gap.get("resolved")


def _gap_covered(gap: dict, covered: set) -> bool:
    return gap.get("claim_key") in covered or bool(gap.get("resolved"))


def _pick_topic(mats: List[dict], topics: Optional[List[dict]]) -> str:
    """选题主题: 优选题库里的选题(按分数), 其次取**最新一批素材**的主体。

    不能简单取"出现次数最多的主体"——历史层与当年层讲的是同一个系列活动, 但只有
    当年层才该出稿(design/11 §10: 语料成套的目的是让 stale/往届关联可判定)。
    """
    if topics:
        best = sorted(topics, key=lambda t: -float(t.get("score") or 0))[0]
        if best.get("subject"):
            return best["subject"]
    from collections import Counter
    latest_year = ""
    for m in mats:
        y = _year_of(m)
        if y and y > latest_year:
            latest_year = y
    fresh = [m for m in mats if not latest_year or _year_of(m) == latest_year]
    subs = [m.get("subject") for m in (fresh or mats) if m.get("subject")]
    if not subs:
        return ""
    return Counter(subs).most_common(1)[0][0]


def _year_of(m: dict) -> str:
    p = m.get("period")
    start = ""
    if isinstance(p, dict):
        start = str(p.get("start") or "")
    if not start:
        pp = Period.parse(m.get("content") or "")
        start = pp.start if pp else ""
    return start[:4] if len(start) >= 4 and start[:4].isdigit() else ""


def _order_materials(mats: List[dict]) -> List[dict]:
    """素材排序: **当年层优先**, 组内按权威度降序、录入序稳定。

    取值路径按顺序取"还没用过的", 所以顺序即优先级。往届素材不是不用, 而是排在后面:
    "背景/沿革"这类段落本来就该引用往届资料, 但导语/过程必须用今年的事实。
    """
    if not mats:
        return []
    years = [_year_of(m) for m in mats]
    known = [y for y in years if y]
    latest = max(known) if known else ""
    if not latest:
        return list(mats)
    fresh = [(i, m) for i, m in enumerate(mats) if _year_of(m) == latest]
    older = [(i, m) for i, m in enumerate(mats) if _year_of(m) != latest]
    fresh.sort(key=lambda x: (-float(x[1].get("authority") or 0.0), x[0]))
    older.sort(key=lambda x: (-float(x[1].get("authority") or 0.0), x[0]))
    return [m for _, m in fresh] + [m for _, m in older]


def _period_key(m: dict) -> str:
    p = m.get("period")
    if isinstance(p, dict) and p.get("start"):
        return str(p["start"])
    pp = Period.parse(m.get("content") or "")
    return pp.start if (pp and pp.start) else ""


def _cite_edges(segments: List[dict], mats: List[dict]) -> List[dict]:
    """成稿时登记的 cites 边(段落 × 素材), 与核验器推导的边合流。"""
    edges: List[dict] = []
    for seg in segments:
        for eid in seg.get("source_refs") or []:
            ev = _by_eid(mats, eid)
            if ev is None:
                continue
            edges.append({"key": f"{seg['seg_id']}>{eid}", "src": seg["seg_id"],
                          "tgt": eid, "kind": "declared", "weight": 1.0,
                          "claim": "", "assertion": "成稿时登记的来源",
                          "similarity": 1.0})
    return edges


def _material_level_gaps(mats, window, detect_conflicts, detect_sensitive,
                         is_isolated, is_stale) -> List[dict]:
    """素材级缺口(素材库自检): 矛盾 / 过期 / 孤证 / 涉隐私 —— 用于"无稿"时的选题驱动。"""
    from content.fact_verifier import GAP_PRIORITY
    gaps: List[dict] = []
    for conf in detect_conflicts([m for m in mats], window):
        cands = conf.get("candidates") or []
        gaps.append({"weakness_type": "conflict", "claim": conf.get("reason", ""),
                     "assertion": conf.get("assertion", ""),
                     "claim_key": conf.get("claim") or conf.get("reason"),
                     "source_eid": (cands[0] or {}).get("eid") if cands else "",
                     "priority": GAP_PRIORITY["conflict"], "blocking": True,
                     "reason": conf.get("reason", ""), "candidates": cands,
                     "seg_id": ""})
    for m in mats:
        if is_stale(m, window):
            gaps.append({"weakness_type": "stale", "claim": m.get("content", ""),
                         "assertion": _period_text(m), "source_eid": m.get("eid"),
                         "claim_key": f"stale:{m.get('eid')}",
                         "priority": GAP_PRIORITY["stale"], "blocking": False,
                         "reason": "往届信息, 勿直接引用", "seg_id": ""})
        if is_isolated(m, mats):
            gaps.append({"weakness_type": "isolated", "claim": m.get("content", ""),
                         "assertion": m.get("subject") or "",
                         "source_eid": m.get("eid"),
                         "claim_key": f"isolated:{m.get('eid')}",
                         "priority": GAP_PRIORITY["isolated"], "blocking": False,
                         "reason": "仅单一低权威来源", "seg_id": ""})
        for h in detect_sensitive(m.get("content") or ""):
            gaps.append({"weakness_type": "sensitive",
                         "claim": m.get("content", ""), "assertion": h["sample"],
                         "source_eid": m.get("eid"),
                         "claim_key": f"sensitive:{m.get('eid')}:{h['sample']}",
                         "priority": GAP_PRIORITY["sensitive"], "blocking": True,
                         "reason": f"命中隐私模式: {h['pattern']}", "seg_id": ""})
    return gaps


__all__ = ["ContentEngine", "SEGMENTS", "SEGMENT_LABELS", "TEMPLATES",
           "SEGMENT_MATERIAL_TYPES", "S_SEG_PENDING", "S_SEG_DRAFTED",
           "S_SEG_VERIFIED", "S_SEG_BLOCKED"]
