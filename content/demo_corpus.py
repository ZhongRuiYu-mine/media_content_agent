# coding: utf-8
"""content/demo_corpus.py — 演示语料(design/11 §10): 历史层 + 当年层。

**为什么必须成套**: 只给新料演示不出核心能力——`stale`(过期)、孤证、往届关联
三类判定都需要"往届"的素材与稿件作对照(design/11 §10 开篇与 §12 R8)。

预期演示结果(design/11 §10 末行):
    缺口清单 无据 3 / 矛盾 1 / 孤证 1 / 过期 1 / 涉隐私 1

这里的语料是虚构的(C4: 自备语料), 机构名用泛称, 人名只用姓氏+称呼, 不含真实隐私。
嵌入的"坑"写在每条的 `pit` 字段里, 便于答辩时逐条指认。
"""
from __future__ import annotations

from typing import List

CLUB_ID = "demo_club"
CLUB_NAME = "计算机学院学生科技社团"

# ---------- 历史层(上一届, 用于建立往届关联边与风格参照) ----------
HISTORY_MATERIALS: List[dict] = [
    {
        "type": "notice", "subject": "上届编程马拉松比赛",
        "content": "2024年11月16日, 计算机学院学生科技社团在大学生活动中心举办上届编程马拉松比赛, 报名截止2024年11月10日, 参赛队伍24支。",
        "source": "official_doc", "period": {"start": "2024-11", "end": None,
                                             "precision": "month", "raw": "2024年11月16日"},
        "pit": "与今年通知同主体 → 建立 temporal 往届边; 也是 stale 的时间锚点",
    },
    {
        "type": "background", "subject": "社团沿革与往届情况",
        "content": "本社团隶属于计算机学院团委, 章程规定每年秋季举办编程马拉松比赛, 往届情况见历年推文归档。",
        "source": "official_doc", "period": None,
        "pit": "背景资料; 机构名与今年一致(不触发 stale)",
    },
    {
        "type": "activity", "subject": "上届编程马拉松现场",
        "content": "2024年11月16日上届编程马拉松比赛在大学生活动中心举行, 现场照片说明: 选手在大学生活动中心大厅签到入场。",
        "source": "on_site", "period": {"start": "2024-11", "end": None,
                                        "precision": "month", "raw": "2024年11月16日"},
        "pit": "场地名与今年不同(活动中心 vs 报告厅) → 演示 R7 地点变更时对照用",
    },
]

# 往届已成稿的稿件(风格参照 + "往届有今年无"判定的依据)
HISTORY_DRAFT = {
    "topic": "上届编程马拉松比赛",
    "segments": [
        {"seg_id": "hist_lead", "kind": "lead",
         "text": "2024年11月16日, 上届编程马拉松比赛在大学生活动中心举行, 参赛队伍24支。",
         "source_refs": ["evt_000001"]},
    ],
}

# ---------- 当年层(本次演示主体) ----------
CURRENT_MATERIALS: List[dict] = [
    {
        "type": "notice", "subject": "本届编程马拉松比赛",
        "content": "关于举办本届编程马拉松比赛的通知: 11月20日在第三报告厅举办本届编程马拉松比赛, 由计算机学院学生科技社团主办, 报名截止11月15日, 请各参赛队伍准时到场。",
        "source": "official_doc",
        "period": {"start": "2025-11", "end": None, "precision": "day",
                   "raw": "11月20日"},
        "pit": "时间/地点/主办方基线(权威度 1.00)",
    },
    {
        "type": "notice", "subject": "群公告",
        "content": "群公告(负责人发布): 本届编程马拉松比赛预计约200人参加, 报名队伍共12支, 志愿者共50人到场支持。",
        "source": "organizer",
        "period": {"start": "2025-11", "end": None, "precision": "day", "raw": "11月20日"},
        "pit": "\"约200人参加\"(主办方口径 0.85) 与签到表 183 人(现场记录 0.70) 构成 R3 矛盾; "
               "两边权威度不同, 所以系统能先给出一个可复核的裁定, 但仍并列两版由用户定",
    },
    {
        "type": "activity", "subject": "签到表摘录",
        "content": "签到表摘录: 本届编程马拉松比赛实际到场183人, 其中参赛队员96人, 观众87人。",
        "source": "on_site",
        "period": {"start": "2025-11", "end": None, "precision": "day", "raw": "11月20日"},
        "pit": "183 人 → 与群公告 200 人冲突(矛盾候选的另一版本, 权威度 0.70)",
    },
    {
        "type": "activity", "subject": "现场照片说明一",
        "content": "现场照片说明一: 上午9点参赛队伍在第三报告厅完成签到, 选手在第三报告厅调试设备。",
        "source": "on_site",
        "period": {"start": "2025-11", "end": None, "precision": "day", "raw": "11月20日"},
        "pit": "支撑\"过程\"段: 场地与通知一致",
    },
    {
        "type": "activity", "subject": "现场照片说明二",
        "content": "现场照片说明二: 指导老师王老师在第三报告厅为参赛队伍做赛前动员, 社长李同学主持开场。",
        "source": "on_site",
        "period": {"start": "2025-11", "end": None, "precision": "day", "raw": "11月20日"},
        "pit": "人物素材: 提供已知人名(王老师/李同学)供实体核验命中",
    },
    {
        "type": "activity", "subject": "现场照片说明三",
        "content": "现场照片说明三: 下午颁奖环节在第三报告厅举行, 共评出一等奖3支队伍、二等奖5支队伍。",
        "source": "on_site",
        "period": {"start": "2025-11", "end": None, "precision": "day", "raw": "11月20日"},
        "pit": "奖项数字有两个(3 支/5 支), 但不是同一指标键, 不应误判矛盾",
    },
    {
        "type": "person", "subject": "采访录音转写",
        "content": "采访录音转写(干事口述): 这次比赛的参与度很高, 不少同学说是有意思的一届, 现场气氛特别好。",
        "source": "member",
        "period": {"start": "2025-11", "end": None, "precision": "day", "raw": "11月20日"},
        "pit": "唯一支撑只来自 0.50 低权威源 → 该素材构成 isolated(孤证, 警告)",
    },
    {
        "type": "notice", "subject": "报名截图(转述)",
        "content": "报名截图(转述): 想报名的同学请联系负责人张同学, 手机13812345678, 或到3号宿舍楼412室现场咨询。",
        "source": "hearsay",
        "period": {"start": "2025-11", "end": None, "precision": "month", "raw": "11月"},
        "pit": "含联系方式与住址 → 触发 sensitive(阻断)",
    },
]

# 演示用的"素材不足"碎句(阶段 1 验收 A1: 一段散料进去, 逐句标色)
SCATTERED_TEXT = (
    "本届编程马拉松比赛将于11月20日在第三报告厅举行，报名截止11月15日。"
    "负责人说预计约200人参加。"
    "签到表显示实际到场183人。"
    "这次比赛反响热烈，是最好的一届。"
    "想咨询的同学请联系张同学，电话13812345678。"
    "上届比赛2024年11月在大学生活动中心举行。"
)


def all_materials() -> List[dict]:
    """历史层 + 当年层(录入顺序即 seq 顺序: 先历史, 后当年)。"""
    return [dict(m) for m in (HISTORY_MATERIALS + CURRENT_MATERIALS)]


def current_only() -> List[dict]:
    return [dict(m) for m in CURRENT_MATERIALS]


# 演示稿件(嵌入 design/11 §10 表里那几类坑: 无据/矛盾/过期/涉隐私)
DEMO_DRAFT_SEGMENTS = [
    {"seg_id": "lead", "kind": "lead",
     "text": "11月20日, 本届编程马拉松比赛在第三报告厅举行, 由计算机学院学生科技社团主办。"},
    {"seg_id": "background", "kind": "background",
     "text": "本社团隶属于计算机学院团委, 每年秋季举办编程马拉松比赛。"},
    {"seg_id": "process", "kind": "process",
     "text": "上午9点参赛队伍在第三报告厅完成签到, 指导老师王老师到场做赛前动员。"},
    {"seg_id": "highlight", "kind": "highlight",
     "text": "本次比赛反响热烈、好评如潮, 到场人数超过300人。"},
    {"seg_id": "highlight2", "kind": "highlight",
     "text": "负责人介绍本届比赛预计约200人参加。"},
    {"seg_id": "impact", "kind": "impact",
     "text": "上届比赛于2024年11月16日在大学生活动中心举行, 参赛队伍24支。"},
    {"seg_id": "closing", "kind": "closing",
     "text": "报名咨询请联系张同学, 电话13812345678。"},
]

__all__ = ["CLUB_ID", "CLUB_NAME", "HISTORY_MATERIALS", "CURRENT_MATERIALS",
           "HISTORY_DRAFT", "SCATTERED_TEXT", "DEMO_DRAFT_SEGMENTS",
           "all_materials", "current_only"]
