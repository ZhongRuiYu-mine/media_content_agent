# coding: utf-8
"""content/assist.py — 六角色"智能辅助"(确定性优先, 每个角色给意见 + 可一键应用的改动)。

和 `components/review_runtime.py` 的「采编复盘」是两个东西, 别混:

  - 采编复盘: 多智能体团队开会(要 LLM), 产出对话式复盘, 供人读。
  - 本模块: **用户勾几个角色就只跑几个**, 零 LLM 就有结果, 直接对着中栏的积木流出意见,
    并且把"要改哪里"变成一条可应用的 action(人在环上: 用户点「应用」才改稿)。

六角色各管一摊(design/11 §9 T3.1 的定位, 这里落成确定性实现):

| 角色 | 确定性职责 | 可应用的改动 |
|---|---|---|
| fact_checker       | 逐块跑断言级核验, 报缺口 | 删掉整块无据的块 |
| compliance_reviewer| 隐私模式 + 绝对化/效果承诺用词 | 脱敏替换 |
| copy_editor        | 超长块按句拆开(移动端阅读) | 拆句 |
| platform_operator  | 三渠道版式能不能装下(用 preview) | 收敛到上限内 |
| impact_analyst     | 来源结构/现场感能不能撑起传播(不编数据) | 无(只给观察点) |
| review_host        | 汇总成"先解决哪一件事" | 无 |

**确定性优先**: 没有 API Key / 闸门不放行时, 上面这些照跑, 结果一样可演示。
LLM 只可能在"改完之后更好听"这一层加分, 不参与"能不能站住"的判断。
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence

from content.fact_verifier import (
    _SENSITIVE_PATTERNS, activity_window, detect_sensitive, verify_draft,
)
from content.material_ops import SUBJECTIVE_MARKERS
from content.preview import preview_channels

# 角色顺序 = 发言顺序(主持最后汇总)
ROLES: List[dict] = [
    {"id": "fact_checker", "label": "事实核查员",
     "desc": "逐块核对数字/人名/职务/时间/地点在素材里有没有依据"},
    {"id": "compliance_reviewer", "label": "合规风险审查",
     "desc": "隐私信息、绝对化表述、未证实的效果承诺"},
    {"id": "copy_editor", "label": "文案编辑",
     "desc": "长句拆开、压到平台字数内, 不动事实"},
    {"id": "platform_operator", "label": "多平台运营",
     "desc": "同一稿在订阅号/短视频/海报三个渠道能不能装下"},
    {"id": "impact_analyst", "label": "传播效果分析师",
     "desc": "来源结构与现场感够不够撑传播, 给可核对的观察点"},
    {"id": "review_host", "label": "采编主持",
     "desc": "汇总成\"这一稿最该先解决的一件事\""},
]
ROLE_IDS = [r["id"] for r in ROLES]
ROLE_LABELS = {r["id"]: r["label"] for r in ROLES}

# 绝对化/效果承诺用词(合规视角的确定性词表, 不判断是否违法, 只提示"这句不好证")
ABSOLUTE_WORDS = ("最好", "最强", "第一", "唯一", "史上", "绝对", "保证", "一定能",
                  "100%", "百分之百", "全网", "顶级", "国家级", "永久")

# 移动端阅读的经验阈值: 一块超过这个字数, 订阅号里会变成一坨
LONG_BLOCK_CHARS = 60
# 短视频单条字幕建议字数
SHOT_TEXT_CHARS = 24


def _blocks(segments: Optional[Sequence[dict]]) -> List[dict]:
    out: List[dict] = []
    for i, seg in enumerate(segments or []):
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        out.append({
            "seg_id": seg.get("seg_id") or f"blk_{i + 1}",
            "text": text,
            "eid": (seg.get("source_refs") or [""])[0] if seg.get("source_refs") else "",
            "pos": i + 1,
        })
    return out


def _mask_text(text: str) -> str:
    """把隐私命中串就地替换成 ***, 其余原样(只脱敏, 不改写)。"""
    out = text or ""
    for _, rx in _SENSITIVE_PATTERNS:
        out = rx.sub(lambda m: "***", out)
    return out


def _split_block(text: str) -> List[str]:
    parts = [p.strip() for p in re.split(r"[。！？!?；;]+", text or "") if p.strip()]
    return parts if len(parts) > 1 else []


def _seg(seg_id: str) -> str:
    """blk_3 → 第3块(给用户看的说法)。"""
    m = re.search(r"(\d+)$", seg_id or "")
    return f"第{m.group(1)}块" if m else seg_id


# ---------------------------------------------------------------- 各角色
def _fact_checker(blocks: List[dict], materials: List[dict]) -> dict:
    if not blocks:
        return {"text": "中栏还没有内容，暂时没得核。", "refs": [], "action": None}
    draft = {"topic": "", "segments": [
        {"seg_id": b["seg_id"], "kind": "block", "text": b["text"],
         "source_refs": [b["eid"]] if b["eid"] else []} for b in blocks]}
    res = verify_draft(draft, materials)
    by_type = (res.get("summary") or {}).get("by_type") or {}
    all_gaps = res.get("gaps") or []
    # 块级缺口 = 稿件里真有这句话的; 素材级缺口(孤证/过期/素材间矛盾)只提一句,
    # 否则孤证按素材计数会把"稿件有 4 个问题"说成 13 个。
    seg_gaps = [g for g in all_gaps if g.get("seg_id")]
    mat_gaps = [g for g in all_gaps if not g.get("seg_id")]
    names = {"no_source": "无据", "conflict": "与其他素材矛盾",
             "sensitive": "涉隐私", "isolated": "孤证", "stale": "过期"}

    if not seg_gaps:
        return {"text": f"逐块核过 {len(blocks)} 块：稿件里的说法都能在素材里找到依据，"
                        f"没有待核实项。"
                        + (f"（素材库另有 {len(mat_gaps)} 条待核实，不在稿件里，"
                           f"见待核实清单）" if mat_gaps else ""),
                "refs": [], "action": None}

    by_seg_type: Dict[str, int] = {}
    for g in seg_gaps:
        k = g.get("weakness_type") or "?"
        by_seg_type[k] = by_seg_type.get(k, 0) + 1
    blocking = sum(1 for g in seg_gaps if g.get("blocking"))
    counted = "、".join(f"{names.get(k, k)} {v}" for k, v in by_seg_type.items())
    lines = [f"逐块核过 {len(blocks)} 块：{len(seg_gaps)} 处缺口（{counted}）" +
             (f"，其中 {blocking} 处阻断发布" if blocking else "") + "。"]
    # 只挑稿件里真有这句话的缺口举例
    for g in seg_gaps[:3]:
        lines.append(f"· {_seg(g.get('seg_id'))}「{_clip(g.get('assertion') or '', 22)}」"
                     f"—— {names.get(g.get('weakness_type'), g.get('weakness_type'))}："
                     f"{_clip(g.get('reason') or '', 40)}")
    if mat_gaps:
        lines.append(f"另有 {len(mat_gaps)} 条素材级问题（孤证/过期/素材之间说法不一），"
                     f"不在这一稿里，去「待核实清单」看。")

    # 可应用: 整块完全无据(该块有 no_source 且没有任何引用) → 建议删掉
    dropped: List[str] = []
    for b in blocks:
        mine = [g for g in seg_gaps if g.get("seg_id") == b["seg_id"]]
        if not b["eid"] and any(g.get("weakness_type") == "no_source" for g in mine):
            dropped.append(b["seg_id"])
    action = None
    if dropped:
        action = {"type": "drop_blocks", "seg_ids": dropped,
                  "label": f"删掉 {len(dropped)} 块（素材里找不到依据）"}
    refs = list(dict.fromkeys(g.get("seg_id") for g in seg_gaps[:6] if g.get("seg_id")))
    return {"text": "\n".join(lines), "refs": refs, "action": action}


def _compliance(blocks: List[dict]) -> dict:
    hits: List[dict] = []
    absolutes: List[dict] = []
    for b in blocks:
        sens = detect_sensitive(b["text"])
        if sens:
            hits.append({"seg_id": b["seg_id"], "hits": sens,
                         "masked": _mask_text(b["text"])})
        low = b["text"]
        words = [w for w in ABSOLUTE_WORDS if w in low]
        words += [m for m in SUBJECTIVE_MARKERS if m in low]
        if words:
            absolutes.append({"seg_id": b["seg_id"], "words": sorted(set(words))})
    if not hits and not absolutes:
        return {"text": f"过了 {len(blocks)} 块：没有隐私串，也没有绝对化/效果承诺用词。",
                "refs": [], "action": None}
    lines: List[str] = []
    if hits:
        lines.append(f"{len(hits)} 块命中隐私模式（电话/学号/住址/房间号这类），"
                     f"发布前必须脱敏：")
        for h in hits[:3]:
            pats = "、".join(sorted({x["pattern"] for x in h["hits"]}))
            lines.append(f"· {_seg(h['seg_id'])} {pats}：{h['hits'][0]['sample']}")
    if absolutes:
        lines.append(f"{len(absolutes)} 块有不好证的用词（不违法也要能说清依据）：")
        for a in absolutes[:3]:
            lines.append(f"· {_seg(a['seg_id'])} " + "、".join(a["words"][:4]))
    action = None
    if hits:
        action = {"type": "mask_spans", "items": [
            {"seg_id": h["seg_id"], "text": h["masked"]} for h in hits],
            "label": f"脱敏 {len(hits)} 块（命中串替换成 ***）"}
    return {"text": "\n".join(lines),
            "refs": [h["seg_id"] for h in hits] + [a["seg_id"] for a in absolutes][:3],
            "action": action}


def _copy_editor(blocks: List[dict], preview: dict) -> dict:
    longb = [b for b in blocks if len(b["text"]) > LONG_BLOCK_CHARS]
    empty = (preview.get("stats") or {}).get("empty")
    if empty:
        return {"text": "中栏还没有内容，先拖素材进来再谈表达。", "refs": [], "action": None}
    shots = [(s["no"], s["text"]) for s in
             (preview["channels"]["shortvideo"].get("shots") or []) if s.get("text")]
    longshots = [no for no, t in shots if len(t) > SHOT_TEXT_CHARS]
    lines = [f"{len(blocks)} 块、共 {(preview.get('stats') or {}).get('chars', 0)} 字。"]
    if longb:
        lines.append(f"{len(longb)} 块超过 {LONG_BLOCK_CHARS} 字，手机上一屏读不完，"
                     f"建议按句拆开：")
        for b in longb[:3]:
            lines.append(f"· {_seg(b['seg_id'])} {len(b['text'])} 字")
    if longshots:
        lines.append(f"短视频里镜{'、镜'.join(str(n) for n in longshots[:3])} "
                     f"单条字幕超 {SHOT_TEXT_CHARS} 字，读字幕会赶不上。")
    if not longb and not longshots:
        lines.append("句子长短都还行：没有超长块，短视频字幕也读得过来。")
    # 可应用: 长块 + 超长字幕块都按句拆(拆不出句子的不参与)
    targets = {b["seg_id"]: b for b in longb}
    for no, t in shots:
        if len(t) > SHOT_TEXT_CHARS:
            for b in blocks:
                if b["text"] == t:
                    targets[b["seg_id"]] = b
    items = [{"seg_id": sid, "pieces": _split_block(b["text"])}
             for sid, b in targets.items()]
    items = [x for x in items if x["pieces"]]
    action = None
    if items:
        action = {"type": "split_blocks", "items": items,
                  "label": f"按句拆开 {len(items)} 块（长句与超长字幕）"}
    elif targets:
        # 有长句但没有句末标点 → 拆不出来。说清楚, 不装作有办法
        lines.append("这几块没有句号可断，我拆不了——断句会改变语气，留给你手动分。")
    return {"text": "\n".join(lines), "refs": [x["seg_id"] for x in items],
            "action": action}


def _platform_ops(preview: dict) -> dict:
    ch = preview["channels"]
    w, s, p = ch["wechat"], ch["shortvideo"], ch["poster"]
    lines = [
        f"订阅号 {w['limit']['count']}/{w['limit']['limit']} 字"
        + ("（超了，发布时会按句收敛）" if w["limit"]["over"] else "，装得下"),
        f"短视频 {s['limit']['count']}/{s['limit']['limit']} 字，"
        + (f"还有 {len(s['missing'])} 个空分镜" if s["missing"] else "6 个分镜都填上了"),
        f"海报 {p['limit']['count']}/{p['limit']['limit']} 字，"
        + (f"缺 {'、'.join(p['missing'])}" if p["missing"] else "四个元素齐了"),
    ]
    action = None
    over = [k for k in ("wechat", "shortvideo") if ch[k]["limit"]["over"]]
    if over:
        # 收敛建议: 保留头部若干块(导语优先), 具体的"留几块"由确定性试算得出
        keep = _fit_keep(preview, over[0])
        if keep and 0 < keep < (preview.get("stats") or {}).get("blocks", 0):
            action = {"type": "trim_to", "keep": keep,
                      "label": f"只留前 {keep} 块（{ch[over[0]]['label']}上限内）"}
    return {"text": "\n".join(lines), "refs": [], "action": action}


def _fit_keep(preview: dict, platform: str) -> int:
    """试算: 前 k 块渲染后不再超上限的最小 k。确定性, 不猜。"""
    total = (preview.get("stats") or {}).get("blocks", 0)
    segs = preview.get("_segments") or []
    if not segs:
        return 0
    for k in range(1, total + 1):
        one = preview_channels(segs[:k])["channels"][platform]
        if not one["limit"]["over"]:
            return k
    return 1


def _impact(blocks: List[dict], materials: List[dict], preview: dict) -> dict:
    by_id = {m.get("eid"): m for m in materials or []}
    sourced = [b for b in blocks if b["eid"]]
    bands: Dict[str, int] = {}
    for b in sourced:
        band = ((by_id.get(b["eid"]) or {}).get("badge") or {}).get("band") or "unknown"
        bands[band] = bands.get(band, 0) + 1
    band_cn = {"official": "官方文件", "organizer": "主办方", "on_site": "现场",
               "member": "口述", "hearsay": "转述", "unknown": "未标"}
    lines = [
        f"这一稿 {len(blocks)} 块，{len(sourced)} 块挂得上来源"
        + (f"（{'、'.join(f'{band_cn.get(k, k)} {v}' for k, v in bands.items())}）"
           if bands else "") + "。",
    ]
    weak = len(blocks) - len(sourced)
    if weak:
        lines.append(f"{weak} 块没有来源指针：读者质疑时你拿不出依据，传播越大越被动。")
    on_site = bands.get("on_site", 0)
    if not on_site:
        lines.append("没有一条现场记录来源：短视频的第 3/4 镜（现场亮点、人物出镜）"
                     "目前只能靠文字撑，补一张现场照片说明会明显不一样。")
    else:
        lines.append(f"有 {on_site} 块现场来源，短视频的现场感撑得住。")
    lines.append("可核对的观察点：发布后对比「有来源的块」与「无来源的块」"
                 "在评论区的追问比例——这是能验证的判断，不是预估数字。")
    return {"text": "\n".join(lines), "refs": [b["seg_id"] for b in blocks if not b["eid"]],
            "action": None}


def _host(msgs: List[dict], preview: dict, materials: List[dict]) -> dict:
    """汇总: 从各角色意见里挑出"最该先解决的一件事"(确定性优先级)。"""
    priority = ["fact_checker", "compliance_reviewer", "platform_operator", "copy_editor"]
    first = None
    for pid in priority:
        for m in msgs:
            if m["role"] == pid and m.get("action"):
                first = m
                break
        if first:
            break
    n_action = sum(1 for m in msgs if m.get("action"))
    stats = preview.get("stats") or {}
    head = (f"这一稿 {stats.get('blocks', 0)} 块、{stats.get('chars', 0)} 字，"
            f"{len(materials)} 条素材在库里。")
    if first:
        body = (f"最该先解决的一件事：{ROLE_LABELS.get(first['role'], first['role'])}"
                f"提的那条 —— {first['action']['label']}。"
                f"改完再看一遍预览，比一次性全改更容易判断改对没有。")
    else:
        body = "各角色都没提出必须改的地方：可以直接核验发布了。"
    tail = f"本轮共 {n_action} 条可一键应用的改动。" if n_action else \
        "本轮没有可一键应用的改动，意见仅作参考。"
    return {"text": "\n".join([head, body, tail]), "refs": [], "action": None}


def _clip(s: str, n: int) -> str:
    t = (s or "").strip()
    return t if len(t) <= n else t[:n] + "…"


# ---------------------------------------------------------------- 入口
def run_assist(segments: Optional[Sequence[dict]], roles: Optional[Sequence[str]] = None,
               materials: Optional[Sequence[dict]] = None,
               club_id: str = "default") -> dict:
    """按选中角色产出意见。roles 为空 = 全跑(六个)。

    返回 {messages:[{role, label, text, refs, action, action_label}], llm_used, roles}
    """
    mats = list(materials or [])
    blocks = _blocks(segments)
    picked = [r for r in (roles or ROLE_IDS) if r in ROLE_IDS] or list(ROLE_IDS)
    preview = preview_channels(list(segments or []))
    # _fit_keep 需要原始 segments 来试算
    preview["_segments"] = list(segments or [])

    out: List[dict] = []
    for rid in picked:
        if rid == "review_host":
            continue                      # 主持最后汇总, 保证它看到别人的意见
        if rid == "fact_checker":
            r = _fact_checker(blocks, mats)
        elif rid == "compliance_reviewer":
            r = _compliance(blocks)
        elif rid == "copy_editor":
            r = _copy_editor(blocks, preview)
        elif rid == "platform_operator":
            r = _platform_ops(preview)
        elif rid == "impact_analyst":
            r = _impact(blocks, mats, preview)
        else:
            continue
        out.append({"role": rid, "label": ROLE_LABELS[rid], "kind": "deterministic",
                    "text": r["text"], "refs": r.get("refs") or [],
                    "action": r.get("action"),
                    "action_label": (r.get("action") or {}).get("label") if r.get("action") else ""})

    if "review_host" in picked:
        h = _host(out, preview, mats)
        out.append({"role": "review_host", "label": ROLE_LABELS["review_host"],
                    "kind": "deterministic", "text": h["text"], "refs": [],
                    "action": None, "action_label": ""})

    return {
        "messages": out,
        "llm_used": False,
        "roles": picked,
        "stats": preview.get("stats") or {},
        "club_id": club_id,
        "window": activity_window(mats),
        "note": "六角色为确定性实现：没有 API Key 也照跑，结论可复现。",
    }


__all__ = ["run_assist", "ROLES", "ROLE_IDS", "ROLE_LABELS",
           "ABSOLUTE_WORDS", "LONG_BLOCK_CHARS"]
