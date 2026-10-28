# coding: utf-8
"""content/platform_spec.py — 平台规范与双轨渲染规格(design/11 §5.4)。

三个平台(design/11 §14 O2 暂定三个, 可通过 env 收窄):

| 平台 | 字数上限 | 段落结构 | 备注 |
|---|---|---|---|
| wechat     订阅号   | 2000 | lead/background/process/highlight/impact/closing | 双行主副标题 |
| shortvideo 短视频脚本 | 300  | hook/conflict/proof/cta | 6 个分镜 |
| poster     海报文案   | 60   | headline/sub/time_place | 一眼看完 |

规范是**硬约束**: 渲染时字数超限要给出告警(前端右栏显示), 而不是默默截断。
"""
from __future__ import annotations

import os
from typing import Dict, Tuple

# 稿件六段结构(design/11 §5.3 SEGMENTS; 与 content_engine 共用一套口径)
SEGMENTS: Tuple[str, ...] = ("lead", "background", "process", "highlight",
                             "impact", "closing")

SEGMENT_LABELS = {
    "lead": "导语", "background": "背景", "process": "过程",
    "highlight": "亮点", "impact": "影响", "closing": "结语",
}

# 平台规格(design/11 §5.4 原表)
PLATFORM_SPECS: Dict[str, dict] = {
    "wechat": {
        "label": "订阅号",
        "max_chars": 2000,
        "segments": SEGMENTS,
        "title_style": "双行主副标题",
        "shots": 0,
    },
    "shortvideo": {
        "label": "短视频脚本",
        "max_chars": 300,
        "segments": ("hook", "conflict", "proof", "cta"),
        "title_style": "一句话钩子",
        "shots": 6,
    },
    "poster": {
        "label": "海报文案",
        "max_chars": 60,
        "segments": ("headline", "sub", "time_place"),
        "title_style": "大字标题 + 一行副题",
        "shots": 0,
    },
}

PLATFORM_LABELS = {k: v["label"] for k, v in PLATFORM_SPECS.items()}

# 短视频分镜模板(6 个分镜, design/11 §5.4 shots=6)
SHOT_TEMPLATE = (
    {"no": 1, "name": "开场钩子", "camera": "近景", "seconds": 3},
    {"no": 2, "name": "活动信息", "camera": "全景", "seconds": 4},
    {"no": 3, "name": "现场亮点", "camera": "中景", "seconds": 5},
    {"no": 4, "name": "人物出镜", "camera": "近景", "seconds": 5},
    {"no": 5, "name": "成果数据", "camera": "特写", "seconds": 4},
    {"no": 6, "name": "号召行动", "camera": "全景", "seconds": 3},
)

# 短视频段 → 分镜映射(hook/conflict/proof/cta 四段落到 6 个分镜上)
SHOT_BY_SEGMENT = {"hook": (1,), "conflict": (2, 3), "proof": (4, 5), "cta": (6,)}

# 海报三段 → 排版位
POSTER_SLOTS = {"headline": "主标题", "sub": "副标题", "time_place": "时间地点"}


def platform_spec(platform: str) -> dict:
    """取平台规格; 未登记平台回退订阅号(不猜上限)。"""
    return PLATFORM_SPECS.get((platform or "").strip(), PLATFORM_SPECS["wechat"])


def platforms() -> Tuple[str, ...]:
    """启用哪些平台(env CONTENT_PLATFORMS 可收窄, design/11 §9 砍单顺序: 3 → 2 个)。"""
    raw = (os.getenv("CONTENT_PLATFORMS") or "").strip()
    if not raw:
        return tuple(PLATFORM_SPECS.keys())
    picked = tuple(p.strip() for p in raw.split(",") if p.strip() in PLATFORM_SPECS)
    return picked or tuple(PLATFORM_SPECS.keys())


def char_count(text: str) -> int:
    """字数统计(中文按字, 空白不计; 与平台上限同一口径)。"""
    return len("".join((text or "").split()))


def over_limit(platform: str, text: str) -> dict:
    """字数是否超上限 → {limit, count, over, ratio}(前端右栏告警用)。"""
    spec = platform_spec(platform)
    n = char_count(text)
    limit = int(spec["max_chars"])
    return {"platform": platform, "label": spec["label"], "limit": limit, "count": n,
            "over": n > limit, "ratio": round(n / limit, 3) if limit else 0.0}


__all__ = [
    "SEGMENTS", "SEGMENT_LABELS", "PLATFORM_SPECS", "PLATFORM_LABELS",
    "SHOT_TEMPLATE", "SHOT_BY_SEGMENT", "POSTER_SLOTS",
    "platform_spec", "platforms", "char_count", "over_limit",
]
