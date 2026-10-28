# coding: utf-8
"""content/preview.py — 三渠道实时预览(零 LLM, 不做门禁)。

为什么单独一层(与 `render.py` 的区别):

  - `render_segments()` 是**发布轨**: 要求稿件有 lead/process/... 六段结构, 且发布前要过核验门禁。
  - 这里服务的是**组装过程中的预览**: 用户在左栏拖积木, 中栏先到在上, 根本没有"段"的概念。
    所以本模块按**位置**映射到渠道版式(第一块当导语、最后一块当结语), 不要求用户分段。

它只做三件事, 全部确定性:

  1. 订阅号: 标题取首块, 正文按积木顺序拼接, 报字数与上限。
  2. 短视频: 6 个分镜模板按块顺序填充, 空分镜**明确标注"还没素材"**, 不编造。
  3. 海报: 从块里抽「主体 / 时间 / 地点 / 一个数字」四个元素排版。
     抽不到的槽位标空并在 notes 里说明; 超 60 字时按**槽位优先级**收敛(先丢副标题),
     而不是整段截断 —— 这是上一版"海报永远 60/60 且 trimmed"的结构性修正。

预览不等于发布: 预览无门禁(还没定稿就该能看见样貌), 发布走 /api/content/platforms。
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from content import platform_spec as spec
from content.material_ops import extract_entities
from graphstore.types import extract_metrics, norm_text

# 时间表达式(海报用): 取最短可读片段, 优先"月日 + 日/号"
_TIME_RES = (
    re.compile(r"\d{1,2}\s*月\s*\d{1,2}\s*[日号]"),
    re.compile(r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*[日号]"),
    re.compile(r"\d{4}\s*年\s*\d{1,2}\s*月"),
    re.compile(r"(?:本周|下周|周|星期)[一二三四五六日天]"),
    re.compile(r"(?:今天|明天|后天|今晚|明晚)"),
)

# 渠道并排顺序(前端按这个顺序渲染三列)
CHANNEL_ORDER = ("wechat", "shortvideo", "poster")


def _blocks(segments: Optional[List[dict]]) -> List[dict]:
    """统一成 [{seg_id, text}] 的积木序列(保持顺序, 丢空块)。"""
    out: List[dict] = []
    for i, seg in enumerate(segments or []):
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        out.append({
            "seg_id": seg.get("seg_id") or f"blk_{i + 1}",
            "text": text,
            "eid": (seg.get("source_refs") or [""])[0] if seg.get("source_refs") else "",
        })
    return out


def _clip(text: str, limit: int) -> str:
    """按字截断(不切标点中间留下的半个词, 只是短界面文案)。"""
    t = (text or "").strip().rstrip("。，,、；;")
    return t if len(t) <= limit else t[:limit]


def _first_time(blocks: List[dict]) -> str:
    for b in blocks:
        for rx in _TIME_RES:
            m = rx.search(b["text"])
            if m:
                return re.sub(r"\s+", "", m.group(0))
    return ""


def _first_venue(blocks: List[dict]) -> str:
    for b in blocks:
        for ent in extract_entities(b["text"]):
            if ent["kind"] == "venue":
                return ent["text"]
    return ""


def _first_subject(blocks: List[dict]) -> str:
    """主体: 优先机构/组织, 其次场地, 最后退回首块前 14 字。"""
    for kind in ("org", "venue"):
        for b in blocks:
            for ent in extract_entities(b["text"]):
                if ent["kind"] == kind:
                    return ent["text"]
    return _clip(blocks[0]["text"] if blocks else "", 14)


# 指标键 → 海报上的中文说法(没有就用原键)
_METRIC_CN = {
    "attendees": "参与", "signups": "报名", "teams": "队伍", "works": "作品",
    "sessions": "场次", "duration": "时长", "views": "阅读", "likes": "点赞",
    "shares": "转发", "comments": "评论", "prize": "奖金", "budget": "经费",
    "cost": "成本", "completion": "完成率", "satisfaction": "满意度",
    "retention": "留存", "conversion": "转化", "rank": "名次", "award": "获奖",
    "score": "成绩", "gpa": "绩点", "accuracy": "准确率", "ctr": "点击率",
}


def _num_text(value) -> str:
    """183.0 → 183(海报上一个数字要干净)。"""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(f)) if abs(f - int(f)) < 1e-9 else f"{f:g}"


def _first_metric(blocks: List[dict]) -> dict:
    """一个数字: 取第一个能抽到的量化断言(带单位, 便于海报一眼看懂)。"""
    for b in blocks:
        for m in extract_metrics(b["text"]):
            v = _num_text(m.value)
            label = _METRIC_CN.get(m.key, m.key)
            return {"value": v, "unit": m.unit or "", "key": m.key,
                    "label": label, "text": f"{label} {v}{m.unit or ''}"}
    return {}


def _count(text: str) -> int:
    return spec.char_count(text)


# ---------------------------------------------------------------- 订阅号
def _wechat(blocks: List[dict]) -> dict:
    title = _clip(blocks[0]["text"], 30) if blocks else ""
    body = "\n\n".join(b["text"] for b in blocks)
    text = (title + "\n\n" + body).strip()
    lim = spec.over_limit("wechat", text)
    notes: List[str] = []
    if not blocks:
        notes.append("中栏还是空的：把左边素材拖进来就有预览")
    if lim["over"]:
        notes.append(f"超上限 {lim['count'] - lim['limit']} 字，发布时会按句边界收敛")
    return {
        "platform": "wechat", "label": "订阅号",
        "title": title or "（还没有标题）",
        "parts": [{"slot": "body", "label": f"第{i + 1}块", "text": b["text"]}
                  for i, b in enumerate(blocks)],
        "text": text, "limit": lim, "notes": notes, "missing": [],
        "trimmed": False,
    }


# ---------------------------------------------------------------- 短视频
def _shortvideo(blocks: List[dict]) -> dict:
    tpl = list(spec.SHOT_TEMPLATE)
    # 位置映射: 第 1 块→镜头1(钩子), 中间块依次铺到镜头 2..5, 最后一块→镜头6(号召)
    assign: Dict[int, dict] = {}
    if blocks:
        assign[0] = blocks[0]
        middle = blocks[1:-1] if len(blocks) > 2 else blocks[1:]
        for k, b in enumerate(middle[:4]):
            assign[1 + k] = b
        if len(blocks) >= 3:
            assign[5] = blocks[-1]
        elif len(blocks) == 2:
            assign[1] = blocks[1]
    shots: List[dict] = []
    missing: List[str] = []
    for i, t in enumerate(tpl):
        b = assign.get(i)
        if b is None:
            missing.append(f"镜{t['no']}·{t['name']}")
        shots.append({**t, "text": (b or {}).get("text", ""),
                      "source_refs": [(b or {}).get("eid", "")] if b else []})
    lines = [f"[镜{s['no']}·{s['name']} {s['seconds']}s] {s['text'] or '（还没素材）'}"
             for s in shots]
    text = "\n".join(lines)
    lim = spec.over_limit("shortvideo", text)
    notes: List[str] = []
    if missing:
        notes.append("空分镜：" + "、".join(missing) + "（不加素材就一直空着，不编造画面）")
    if lim["over"]:
        notes.append(f"超上限 {lim['count'] - lim['limit']} 字，发布时会按句边界收敛")
    return {
        "platform": "shortvideo", "label": "短视频脚本",
        "title": _clip(blocks[0]["text"], 24) if blocks else "（还没有钩子）",
        "parts": [{"slot": "shot", "label": f"镜{s['no']}·{s['name']}",
                   "text": s["text"], "seconds": s["seconds"], "camera": s["camera"]}
                  for s in shots],
        "shots": shots, "text": text, "limit": lim, "notes": notes,
        "missing": missing, "trimmed": False,
    }


# ---------------------------------------------------------------- 海报
POSTER_SLOT_LABEL = {"headline": "主标题·主体", "sub": "副标题·一个数字",
                     "time_place": "时间 · 地点"}


def _poster(blocks: List[dict]) -> dict:
    """海报: 主体 / 一个数字 / 时间地点 三槽位硬约束在 60 字内。

    收敛顺序(而不是整段截断): 先丢副标题(数字), 再压时间地点, 最后才裁主标题。
    每丢一个槽位都在 notes 里写明丢了什么、为什么 —— 预览要可解释。
    """
    subject = _first_subject(blocks)
    metric = _first_metric(blocks)
    t = _first_time(blocks)
    v = _first_venue(blocks)

    headline = _clip(subject, 14)
    sub = ""
    if metric:
        sub = _clip(metric["text"], 16)
    time_place = " · ".join([x for x in (t, v) if x])

    notes: List[str] = []
    missing: List[str] = []
    if not t:
        missing.append("时间")
    if not v:
        missing.append("地点")
    if not metric:
        missing.append("一个数字")
    if not blocks:
        notes.append("中栏还是空的：把左边素材拖进来就有预览")

    dropped: List[str] = []

    def total() -> int:
        return _count("\n".join(x for x in (headline, sub, time_place) if x))

    if total() > 60 and sub:
        dropped.append(POSTER_SLOT_LABEL["sub"])
        sub = ""
    if total() > 60 and time_place:
        keep = [x for x in (t, v) if x]
        while keep and _count("\n".join([headline] + keep)) > 60:
            keep.pop()
            dropped.append("地点" if len(keep) < len([x for x in (t, v) if x]) else "时间")
        time_place = " · ".join(keep)
    if total() > 60 and headline:
        over = total() - 60
        headline = _clip(headline, max(6, len(headline) - over))
        notes.append(f"主标题为压进 60 字裁到 {len(headline)} 字")

    parts = [
        {"slot": "headline", "label": POSTER_SLOT_LABEL["headline"], "text": headline},
        {"slot": "sub", "label": POSTER_SLOT_LABEL["sub"], "text": sub},
        {"slot": "time_place", "label": POSTER_SLOT_LABEL["time_place"],
         "text": time_place},
    ]
    text = "\n".join(x for x in (headline, sub, time_place) if x)
    lim = spec.over_limit("poster", text)
    if missing:
        notes.append("素材里抽不到：" + "、".join(missing) +
                     "（这几个位置空着，等你在中栏补一句）")
    if dropped:
        notes.append("为压进 60 字按优先级丢弃：" + "、".join(dict.fromkeys(dropped)))
    return {
        "platform": "poster", "label": "海报文案",
        "title": headline or "（还没有主体）",
        "parts": parts, "text": text, "limit": lim, "notes": notes,
        "missing": missing, "trimmed": bool(dropped),
    }


def poster_version(segments: Optional[List[dict]]) -> dict:
    """海报版的发布形态(与 render_segments 同形状, 供 content_runtime 的发布轨复用)。

    上一版海报"永远 60/60 且 trimmed"是因为它把整段正文按上限硬裁。
    这里改成**四元素排版**: 主体 / 一个数字 / 时间地点, 每个槽位有独立字数预算,
    抽不到的槽位留空并写进 notes(结构缺陷看得见, 不是被截断掩盖)。
    """
    p = _poster(_blocks(segments))
    segs = [{
        "seg_id": f"poster_{x['slot']}", "kind": x["slot"],
        "label": x["label"], "text": x["text"], "source_refs": [],
    } for x in p["parts"] if x["text"]]
    return {
        "platform": "poster", "label": p["label"], "track": "base",
        "track_label": "轨一·事实基线", "title": p["title"],
        "segments": segs, "text": p["text"], "limit": p["limit"],
        "trimmed": p["trimmed"], "shots": [], "llm_used": False,
        "notes": p["notes"], "missing": p["missing"],
    }


# ---------------------------------------------------------------- 入口
def preview_channels(segments: Optional[List[dict]]) -> dict:
    """积木流 → 三渠道预览。零 LLM、无门禁、可反复调用(拖一下就重算)。"""
    blocks = _blocks(segments)
    channels = {
        "wechat": _wechat(blocks),
        "shortvideo": _shortvideo(blocks),
        "poster": _poster(blocks),
    }
    return {
        "channels": channels,
        "order": list(CHANNEL_ORDER),
        "stats": {
            "blocks": len(blocks),
            "chars": _count("".join(b["text"] for b in blocks)),
            "sourced": sum(1 for b in blocks if b.get("eid")),
            "empty": not blocks,
        },
    }


__all__ = ["preview_channels", "poster_version", "CHANNEL_ORDER", "POSTER_SLOT_LABEL"]
