# coding: utf-8
"""components/classifier.py — 确定性本地分类器(design/03 落地)。

铁律: 规则 → 本地向量(Qwen3-Embedding-8B, 经 OpenAI 兼容端点) → 用户裁决;
embedding 不可用时规则未命中一律 low_confidence 交用户, 不静默调用云 LLM。

判定矩阵(无主体抽取时按 cosine 带区分):
- max_cos >= SIM_ISOLATED_TOP(0.75): 强相关 → 交由规则(可判重复/细节/冲突)
- SIM_RELATED <= max_cos < SIM_ISOLATED_TOP: 孤立待裁决(isolated)
- max_cos <  SIM_RELATED: 与图无关 → 有信号=new / 无信号=low_confidence
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from graphstore.types import (
    extract_metrics, extract_techs, norm_text,
    subject_similarity, Period,
)

# 分类阈值(env 可覆盖, 见 .env.example)
SIM_DUP = 0.90           # 重复(建议合并)
SIM_RELATED = 0.55       # 相关下限(低于 = 与图无关)
SIM_ISOLATED_TOP = 0.75  # 0.55..0.75 = 孤立/待裁决带; 之上 = 强相关
SUBJECT_SIM_HIT = 0.40   # subject 2-gram 覆盖命中阈值(支持简称)

_SIGNAL_WORDS = (
    "系统 平台 项目 网站 应用 竞赛 比赛 大赛 建模 论文 专利 实习 工作 任职 "
    "学校 大学 学院 本科 硕士 博士 专业 gpa 绩点 岗位 参与 负责 开发 实现 "
    "搭建 设计 做了 做过 使用 采用 用 训练 部署 优化 提升 处理 技术栈".split()
)
# 内容域信号词(design/11 §3.2): 素材里出现这些词说明"这句话是社团活动信息",
# 无主体命中时也应当判 new 而不是 low_confidence(否则一句活动通知会被要求人工裁决)。
_SIGNAL_WORDS_CONTENT = (
    "社团 学生会 团委 协会 联合会 志愿 纳新 招新 讲座 沙龙 分享会 培训 晚会 "
    "路演 展会 开放日 报名 截止 通知 公告 调整 延期 取消 场地 报告厅 礼堂 "
    "操场 体育馆 活动中心 举办 承办 主办 协办 参加 到场 观众 签到 指导老师 "
    "社长 部长 主席 队长 负责人 嘉宾 主持人 获奖 荣获 立项 一等奖 二等奖 三等奖".split()
)
_TECH_EXCLUSIVE = ("主要用", "用的是", "只用", "使用", "技术栈是", "用了")


def _has_signal(text: str) -> bool:
    low = norm_text(text)
    if any(w in low for w in _SIGNAL_WORDS):
        return True
    if any(w in low for w in _SIGNAL_WORDS_CONTENT):
        return True
    # 数字/日期本身就是信号: "183 人" / "11月20日" 这类短句在内容域是有效素材
    if re.search(r"\d", low) and re.search(
            r"[%％人支场次篇天元号月日时分]|参加|到场|报名|举办", low):
        return True
    return False


def _find_matched_events(text: str, events: Dict[str, dict]) -> List[str]:
    """subject 别名/包含匹配(2-gram 覆盖, 见 types.subject_similarity)。"""
    hits = []
    for eid, ev in events.items():
        score = subject_similarity(ev.get("subject", ""), text)
        if score >= SUBJECT_SIM_HIT:
            hits.append((score, eid))
    hits.sort(key=lambda x: x[0], reverse=True)
    return [eid for _, eid in hits]


def _period_conflict(ev: dict, cand: Period) -> Optional[dict]:
    p = Period.from_dict(ev.get("period")) if ev.get("period") else None
    if p is None or p.start is None:
        return None
    if cand.start is None:
        return None
    if p.start != cand.start or (p.end != cand.end):
        return {"rule": "R2", "older_eid": ev["eid"],
                "older_field": "period", "older_raw": p.raw,
                "diff_fields": ["period"]}
    return None


def _metric_conflict(ev: dict, cand_metrics: list) -> Optional[dict]:
    for cm in cand_metrics:
        for em in ev.get("metrics") or []:
            if em.get("key") == cm.key and em.get("kind") == cm.kind \
                    and em.get("value") != cm.value:
                return {"rule": "R3", "older_eid": ev["eid"],
                        "older_field": f"metric.{em['key']}",
                        "older_raw": em.get("raw", ""),
                        "diff_fields": [f"metric.{cm.key}"]}
    return None


def _tech_diff_evidence(ev: dict, cand_techs: list) -> Optional[dict]:
    old_techs = set(ev.get("techs") or [])
    if old_techs and cand_techs and not (old_techs & set(cand_techs)):
        return {"rule": "R4", "older_eid": ev["eid"], "older_field": "techs",
                "older_raw": ",".join(sorted(old_techs)), "diff_fields": ["techs"]}
    return None


# ---------- 内容域: 场地比对(design/11 §3.2 "R2 比对字段换(时间+地点)") ----------
# 场地只有两条素材里都出现、且互不相同时才算"地点变更"; 一条有一條无不算(避免误报)。
_VENUE_TYPES = {"venue"}


def _extract_venues(text: str) -> set:
    """抽场地专名(复用 content.material_ops 的场地词表; 零 LLM)。"""
    from content.material_ops import extract_entities
    return {e["text"] for e in extract_entities(text or "", None)
            if e["kind"] in _VENUE_TYPES}


def _same_venue(a: set, b: set) -> bool:
    """两组场地名是否指向同一地点(含包含关系: "报告厅" ~ "第三报告厅")。"""
    for x in a:
        for y in b:
            if x == y or x in y or y in x:
                return True
    return False


def _venue_conflict(ev: dict, text: str) -> Optional[dict]:
    """R7: 同一主体的地点说法不一致(内容域新增, 仅 enable_content_rules 时启用)。

    只有两边都出现场地名、且互不包含时才算"地点变更"; 一边有一边无不算(避免误报)。
    """
    old = _extract_venues(ev.get("content") or "")
    new = _extract_venues(text or "")
    if old and new and not _same_venue(old, new):
        return {"rule": "R7", "older_eid": ev["eid"], "older_field": "venue",
                "older_raw": "、".join(sorted(old)),
                "diff_fields": ["venue"]}
    return None


def _text_dup_ratio(a: str, b: str) -> float:
    """文本近似度: 归一化后中文二字组集合 Jaccard。"""
    na, nb = norm_text(a), norm_text(b)
    sa = set(re.findall(r"[\u4e00-\u9fff]{2}", na))
    sb = set(re.findall(r"[\u4e00-\u9fff]{2}", nb))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / max(len(sa), len(sb))


def _dup_with_target(events: Dict[str, dict], target_eid, text, cand_vec,
                     vectors, emb_ok, embedder, sim_dup: float) -> bool:
    """重复判定(R1): 文本近似 或 向量相似(目标事件对齐)>= sim_dup。"""
    ev = events.get(target_eid)
    if ev is not None and _text_dup_ratio(ev.get("content", ""), text) >= 0.6:
        return True
    if emb_ok and cand_vec is not None:
        eids = list(events.keys())
        if target_eid in eids:
            vi = eids.index(target_eid)
            if 0 <= vi < len(vectors) and embedder.cosine(cand_vec, vectors[vi]) >= sim_dup:
                return True
    return False


def _rules_verdict(events: Dict[str, dict], target_eid: str, text: str,
                   cand_vec, vectors, emb_ok, embedder,
                   sim_dup: float, content_rules: bool = False) -> dict:
    """针对可归属事件 target_eid 应用 R1-R6 规则(R2/R3 用结构化字段比对)。

    content_rules=True 时额外启用 R7 场地比对(内容域: 时间+地点两个字段都要比,
    见 design/11 §3.2)。默认关闭以保持原有裁决矩阵不动。
    """
    ev = events[target_eid]
    cand_techs = extract_techs(text)
    cand_metrics = extract_metrics(text)
    cand_period = Period.parse(text)

    # 内容域: 地点变更优先于"重复"判定 —— 两届同一活动、只换了场地, 本身确实高度
    # 重复(R1 会命中), 但对用户来说这是"两场不同的活动", 必须并列交用户裁决
    # (design/11 §4.3 conflict: "时间地点与通知不符")。
    if content_rules:
        vc0 = _venue_conflict(ev, text)
        if vc0 is not None:
            return {"candidate": text, "kind": "conflict", "target_eid": target_eid,
                    "evidence": [vc0], "confidence": "high"}

    if _dup_with_target(events, target_eid, text, cand_vec, vectors, emb_ok,
                        embedder, sim_dup):
        return {"candidate": text, "kind": "conflict", "target_eid": target_eid,
                "evidence": [{"rule": "R1", "older_eid": target_eid,
                              "older_field": "content",
                              "older_raw": ev.get("content", ""),
                              "diff_fields": ["content"]}],
                "confidence": "high"}

    if _tech_diff_evidence(ev, cand_techs) and any(
            w in norm_text(text) for w in _TECH_EXCLUSIVE):
        # 技术"改口"疑点: 不自动判矛盾, 交用户(design R4)
        return {"candidate": text, "kind": "low_confidence",
                "target_eid": target_eid,
                "evidence": [_tech_diff_evidence(ev, cand_techs)],
                "confidence": "low"}

    evidence: List[dict] = []
    if cand_period is not None:
        pc = _period_conflict(ev, cand_period)
        if pc:
            evidence.append(pc)
    mc = _metric_conflict(ev, cand_metrics)
    if mc:
        evidence.append(mc)

    if evidence:
        return {"candidate": text, "kind": "conflict", "target_eid": target_eid,
                "evidence": evidence, "confidence": "high"}
    return {"candidate": text, "kind": "detail_of", "target_eid": target_eid,
            "evidence": [{"rule": "R6", "older_eid": target_eid,
                          "older_field": "subject",
                          "older_raw": ev.get("subject", ""), "diff_fields": []}],
            "confidence": "high"}


async def classify(candidate: str, events: Dict[str, dict],
                  embedder=None,
                  sim_dup: float = SIM_DUP,
                  sim_related: float = SIM_RELATED,
                  sim_isolated_top: float = SIM_ISOLATED_TOP,
                  cand_vec=None,
                  event_vectors=None,
                  content_rules: bool = False) -> dict:
    """对单句候选分类(design/03)。

    Returns: {candidate, kind, target_eid, evidence[], confidence}
    kind: new | isolated | detail_of | conflict | low_confidence

    cand_vec / event_vectors: 调用方预计算的向量(批量缓存, 避免每句重嵌全库事件);
    未提供时回退到内部联机嵌入(兼容旧调用)。
    content_rules: 内容域裁决矩阵(启用 R7 场地比对, design/11 §3.2)。
    """
    text = (candidate or "").strip()
    if not text:
        return {"candidate": "", "kind": "low_confidence", "target_eid": None,
                "evidence": [], "confidence": "low"}

    matched = _find_matched_events(text, events)

    # ---- 向量阶段(embedding 可用时) ----
    cand_vec = cand_vec
    vectors: List[List[float]] = []
    max_cos = 0.0
    max_cos_eid = None
    emb_ok = False
    if cand_vec is not None:
        # 调用方预计算: 直接使用缓存向量, 免去全量重嵌
        emb_ok = True
        vectors = event_vectors or []
    elif embedder is not None and embedder.available():
        contents = [norm_text(ev.get("content", "")) for ev in events.values()]
        vecs = await embedder.embed_documents([norm_text(text)] + contents)
        if vecs:
            emb_ok = True
            cand_vec = vecs[0]
            vectors = vecs[1:]
    if emb_ok:
        for (eid, ev), v in zip(events.items(), vectors):
            c = embedder.cosine(cand_vec, v)
            if c > max_cos:
                max_cos = c
                max_cos_eid = eid

    # ---- 有主体命中: 直接规则 ----
    if matched:
        return _rules_verdict(events, matched[0], text, cand_vec, vectors,
                              emb_ok, embedder, sim_dup, content_rules)

    # ---- 无主体命中: 按 cosine 带 ----
    if emb_ok:
        if max_cos >= sim_isolated_top:
            # 强相关(无主体抽取): 交规则(dup/细节/冲突)
            return _rules_verdict(events, max_cos_eid, text, cand_vec, vectors,
                                  emb_ok, embedder, sim_dup, content_rules)
        if max_cos >= sim_related:
            # 弱相关带: 孤立待裁决
            return {"candidate": text, "kind": "isolated", "target_eid": max_cos_eid,
                    "evidence": [{"rule": "SIM", "older_eid": max_cos_eid,
                                  "older_field": "content", "older_raw": "",
                                  "diff_fields": []}], "confidence": "medium"}
        # 与图无关
        if _has_signal(text):
            return {"candidate": text, "kind": "new", "target_eid": None,
                    "evidence": [], "confidence": "high"}
        return {"candidate": text, "kind": "low_confidence", "target_eid": None,
                "evidence": [], "confidence": "low"}

    # ---- embedding 不可用(降级): 规则未命中 → 用户裁决 ----
    return {"candidate": text, "kind": "low_confidence", "target_eid": None,
            "evidence": [], "confidence": "low"}
