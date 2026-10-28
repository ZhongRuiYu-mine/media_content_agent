# coding: utf-8
"""graphstore/types.py — 用户事件图数据模型(design/01 落地)。

约定:
- 持久化/回滚一律使用 JSON 友好 dict(便于快照与重放); 本文件提供 dataclass 骨架与
  纯函数归一化/解析工具(分类器/事实核验复用), 不承载业务状态。
- 素材类型取值见 MATERIAL_TYPES(activity/notice/person/achievement/org/background);
  历史语义 EVENT_TYPES 保留兼容, 新录入素材走 MATERIAL_TYPES。
- 每条素材携带 来源权威度 authority + verified + sensitive, 是事实核验与缺口判定的依据(design/11 §4)。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, List, Optional

# ---------- 事件类型/来源常量 ----------
EDUCATION = "education"
PROJECT = "project"
COMPETITION = "competition"
SKILL = "skill"
ACHIEVEMENT = "achievement"
EXPERIENCE = "experience"
EVENT_TYPES = (EDUCATION, PROJECT, COMPETITION, SKILL, ACHIEVEMENT, EXPERIENCE)

SOURCES = ("resume", "user", "form", "llm_draft")

# ---------- 内容域: 素材类型与来源权威度(design/11 §4.1) ----------
# 素材类型决定它在稿件里落哪个区块; 历史语义(EDUCATION/PROJECT/...)保留兼容,
# 新录入一律走 MATERIAL_TYPES。
ACTIVITY = "activity"          # 活动(比赛/讲座/纳新/志愿/晚会)
NOTICE = "notice"              # 通知公告(报名、场地、时间变更)
PERSON = "person"              # 人物(获奖同学、指导老师、嘉宾)
ORG = "org"                    # 组织(社团、院系、合作单位)
BACKGROUND = "background"      # 背景资料(沿革、章程、往届情况)
MATERIAL_TYPES = (ACTIVITY, NOTICE, PERSON, ACHIEVEMENT, ORG, BACKGROUND)

# 来源通道(扩展原 SOURCES, 与权威度一一对应)
MATERIAL_SOURCES = ("official_doc", "organizer", "on_site", "member", "hearsay")

# 来源权威度: 事实核验的核心依据。0.00-1.00, 决定矛盾/孤证判定时以谁为准。
SOURCE_AUTHORITY = {
    "official_doc": 1.00,   # 盖章文件、官方推文、正式通知
    "organizer": 0.85,      # 主办方邮件、群公告、负责人确认
    "on_site": 0.70,        # 现场照片、视频、签到表
    "member": 0.50,         # 干事口述
    "hearsay": 0.30,        # 转述、截图、朋友圈
}

# 孤证判定阈值: 权威度低于此值且无第二来源 → isolated(design/11 §4.3)
LOW_AUTHORITY = 0.50

# 缺口类型(WeakItem.weakness_type 内容域取值; 原 conflict/isolated/weak_connection 保留兼容)
NO_SOURCE = "no_source"        # 无据: 断言在素材库找不到支撑句
CONFLICT = "conflict"          # 矛盾: 同指标不同值 / 时间地点不符 / 人名职务不一致
ISOLATED = "isolated"          # 孤证: 仅出自单一低权威源
STALE = "stale"                # 过期: period 早于本轮活动窗口
SENSITIVE = "sensitive"        # 涉隐私: 命中电话/身份证/住址/学号
GAP_TYPES = (CONFLICT, NO_SOURCE, SENSITIVE, ISOLATED, STALE)


def authority_of(source: str) -> float:
    """按来源通道取权威度; 未登记的来源按最低档处理(不猜高)。"""
    return float(SOURCE_AUTHORITY.get((source or "").strip(), 0.30))


def authority_band(score: float) -> str:
    """权威度分档(前端徽章用, 与 SOURCE_AUTHORITY 五档一致)。"""
    s = float(score or 0.0)
    if s >= 0.95:
        return "official"
    if s >= 0.80:
        return "organizer"
    if s >= 0.60:
        return "on_site"
    if s >= 0.40:
        return "member"
    return "hearsay"


# ---------- 归一化工具(纯函数, 分类器/比对共用) ----------
_FW = dict((ord(c), ord(a)) for a, c in zip(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789",
    "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ０１２３４５６７８９",
))
_PUNCT_RE = re.compile(r"[\s\u3000\-–—_~.,，。！？!?;；:：、()（）\[\]【】\"'“”‘’]+")


def norm_text(s: str) -> str:
    """全半角/大小写/空白标点归一化(中文保留, 空白标点一律删除)。"""
    if not s:
        return ""
    t = unicodedata.normalize("NFKC", s).translate(_FW)
    t = _PUNCT_RE.sub("", t).strip().lower()
    return t


def _bigrams(s: str) -> set:
    """中文 2-gram 分词(供 subject 别名/近似比对)。"""
    t = norm_text(s)
    if len(t) <= 2:
        return {t} if t else set()
    return {t[i:i + 2] for i in range(len(t) - 1)} | {t}


def subject_similarity(subject: str, text: str) -> float:
    """subject 是否"出现/近似出现"于文本(含简称: 智能推荐系统 ~ 推荐系统)。

    返回 0..1; >= 0.5 视为命中同一主体。英文/技能名走包含判定。
    """
    ns, nt = norm_text(subject), norm_text(text)
    if not ns or not nt:
        return 0.0
    if ns in nt or nt in ns:
        return 1.0
    a, b = _bigrams(subject), _bigrams(text)
    if not a:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    # 覆盖率用 subject 侧: 主题短, 文本侧 bigram 集合大, 归一化到 subject
    return min(1.0, inter / max(1.0, len(a)))


def safe_student(student_id: str) -> str:
    out = "".join(c for c in (student_id or "") if c.isalnum() or c in "-_")
    return out or "default"


def iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------- Period ----------
_YEAR = r"(?:19|20)\d{2}"
_DATE_RE = re.compile(
    rf"({_YEAR})[.\-/年]?\s*(\d{{1,2}})?\s*[月/.\-]?"
    rf"\s*[~\-–—至到]+\s*({_YEAR})[.\-/年]?\s*(\d{{1,2}})?\s*月?"
)
_SINGLE_RE = re.compile(rf"({_YEAR})(?:[.\-/年](\d{{1,2}}))?月?")
# 年份必须带标记(年/. / - / 纯 4 位数)才算年份, 否则 "15" 这类裸数字会被误当年
_YEAR_MARK_RE = re.compile(rf"{_YEAR}\s*(?:[.\-/年]|(?![0-9月]))")
_YEAR_ONLY_RE = re.compile(rf"({_YEAR})(?:[.\-/年](\d{{1,2}}))?")
# 无年份的月日(内容域: 活动通知普遍只写"11月20日"): 值取 "MM-DD", 便于两份素材对齐比较
_MONTH_DAY_RE = re.compile(r"(?<![0-9月])(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
_KEEP_OPEN = ("至今", "现在", "目前", "present")


@dataclass
class Period:
    start: Optional[str] = None
    end: Optional[str] = None
    precision: str = "none"          # day|month|year|none
    raw: str = ""

    @staticmethod
    def parse(raw: str) -> Optional["Period"]:
        if not raw:
            return None
        m = _DATE_RE.search(raw)
        if m:
            start = _to_ym(m.group(1), m.group(2))
            end = _to_ym(m.group(3), m.group(4))
            precision = "month" if (m.group(2) or m.group(4)) else "year"
            return Period(start=start, end=end, precision=precision, raw=raw.strip())
        m = _SINGLE_RE.search(raw)
        if m:
            y = m.group(1)
            mon = m.group(2)
            # 未带 年/./-/ 的裸数字不是年份: 否则 "11月15日" 会被读成 "15-11"
            # (内容域通知普遍只写月日, 见 design/11 §5.1)
            if mon or _YEAR_MARK_RE.search(raw):
                return Period(start=_to_ym(y, mon), end=None,
                              precision="month" if mon else "year", raw=raw.strip())
        # 无年份月日("11月20日"): 值取 "MM-DD", 便于两份素材对齐比较
        m = _MONTH_DAY_RE.search(raw)
        if m:
            return Period(start="%02d-%02d" % (int(m.group(1)), int(m.group(2))),
                          end=None, precision="day", raw=raw.strip())
        if any(k in raw for k in _KEEP_OPEN):
            m = _YEAR_ONLY_RE.search(raw)
            if m:
                return Period(start=_to_ym(m.group(1), m.group(2)), end=None,
                              precision="month" if m.group(2) else "year", raw=raw.strip())
        return None

    def to_dict(self) -> dict:
        return {"start": self.start, "end": self.end,
                "precision": self.precision, "raw": self.raw}

    @staticmethod
    def from_dict(d: Optional[dict]) -> Optional["Period"]:
        if not d:
            return None
        return Period(start=d.get("start"), end=d.get("end"),
                      precision=d.get("precision", "none"), raw=d.get("raw", ""))


def _to_ym(year: Optional[str], month: Optional[str]) -> str:
    if not year:
        return ""
    y = year.strip()
    if month and month.strip():
        return f"{y}-{int(month.strip()):02d}"
    return y


# ---------- Metric / Claim ----------
_METRIC_KEYS = {
    # 内容域(设计/11 §5.1 断言抽取的主战场)
    "参加": "attendees", "参与": "attendees", "到场": "attendees",
    "人数": "attendees", "观众": "attendees", "报名": "signups",
    "队伍": "teams", "作品": "works", "节目": "works",
    "场次": "sessions", "场": "sessions",
    "时长": "duration", "阅读": "views", "浏览": "views", "观看": "views",
    "点赞": "likes", "转发": "shares", "评论": "comments",
    "奖金": "prize", "经费": "budget", "成本": "cost", "完成率": "completion",
    "满意度": "satisfaction", "留存": "retention", "转化": "conversion",
    "排名": "rank", "名次": "rank", "奖项": "award", "获奖": "award",
    "成绩": "score", "绩点": "gpa",
    # 历史指标词(保留兼容)
    "准确率": "accuracy", "精度": "accuracy", "召回": "recall",
    "点击率": "ctr", "qps": "qps", "延迟": "latency", "耗时": "latency",
    "日活": "dau", "交易额": "amount", "用户": "users", "gpa": "gpa",
}
_DELTA_HINTS = ("提升", "提高", "降低", "减少", "增长", "下降")
_TECH_TOKENS = [
    "python", "java", "c++", "c#", "go", "rust", "javascript", "typescript",
    "pytorch", "tensorflow", "springboot", "spring boot", "mysql", "redis",
    "docker", "kafka", "nginx", "git", "linux", "vue", "react", "flask",
    "django", "fastapi", "mongodb", "elasticsearch", "spark", "hadoop",
    "scikit-learn", "sklearn", "numpy", "pandas", "sql", "html", "css",
]
_TECH_SET = set(_TECH_TOKENS)


@dataclass
class Metric:
    key: str
    value: Any
    unit: Optional[str] = None
    kind: str = "absolute"           # delta|absolute
    raw: str = ""

    def to_dict(self) -> dict:
        return {"key": self.key, "value": self.value, "unit": self.unit,
                "kind": self.kind, "raw": self.raw}

    @staticmethod
    def from_dict(d: Optional[dict]) -> Optional["Metric"]:
        if not d:
            return None
        return Metric(key=d.get("key", ""), value=d.get("value"),
                      unit=d.get("unit"), kind=d.get("kind", "absolute"),
                      raw=d.get("raw", ""))


@dataclass
class Claim:
    kind: str
    key: str
    value: str
    raw: str = ""

    def to_dict(self) -> dict:
        return {"kind": self.kind, "key": self.key, "value": self.value, "raw": self.raw}

    @staticmethod
    def from_dict(d: Optional[dict]) -> Optional["Claim"]:
        if not d:
            return None
        return Claim(kind=d.get("kind", "fact"), key=d.get("key", ""),
                     value=d.get("value", ""), raw=d.get("raw", ""))


# ---------- EventRecord ----------
@dataclass
class EventRecord:
    type: str
    subject: str
    content: str
    source: str = "user"
    role: Optional[str] = None
    org: Optional[str] = None
    period: Optional[Period] = None
    keywords: List[str] = field(default_factory=list)
    techs: List[str] = field(default_factory=list)
    metrics: List[Metric] = field(default_factory=list)
    claims: List[Claim] = field(default_factory=list)
    # 内容域: 来源权威度 / 已确认 / 含个人信息(design/11 §4.1)
    authority: float = 0.30
    verified: bool = False
    sensitive: bool = False
    eid: Optional[str] = None
    created_seq: Optional[int] = None
    updated_seq: Optional[int] = None
    mention_count: int = 0
    ts: str = ""

    def to_dict(self) -> dict:
        return {
            "eid": self.eid, "type": self.type, "subject": self.subject,
            "role": self.role, "org": self.org,
            "period": self.period.to_dict() if self.period else None,
            "content": self.content,
            "keywords": self.keywords, "techs": self.techs,
            "metrics": [m.to_dict() for m in self.metrics],
            "claims": [c.to_dict() for c in self.claims],
            "authority": self.authority, "verified": self.verified,
            "sensitive": self.sensitive,
            "source": self.source, "created_seq": self.created_seq,
            "updated_seq": self.updated_seq, "mention_count": self.mention_count,
            "ts": self.ts,
        }

    @staticmethod
    def from_dict(d: dict) -> "EventRecord":
        return EventRecord(
            eid=d.get("eid"), type=d.get("type", PROJECT),
            subject=d.get("subject", ""), role=d.get("role"), org=d.get("org"),
            period=Period.from_dict(d.get("period")),
            content=d.get("content", ""), keywords=list(d.get("keywords") or []),
            techs=list(d.get("techs") or []),
            metrics=[m for m in (Metric.from_dict(x) for x in d.get("metrics") or []) if m],
            claims=[c for c in (Claim.from_dict(x) for x in d.get("claims") or []) if c],
            authority=d.get("authority", authority_of(d.get("source", "user"))),
            verified=bool(d.get("verified", False)),
            sensitive=bool(d.get("sensitive", False)),
            source=d.get("source", "user"), created_seq=d.get("created_seq"),
            updated_seq=d.get("updated_seq"), mention_count=int(d.get("mention_count") or 0),
            ts=d.get("ts", ""),
        )


# ---------- WeakMemoryItem(graphstore 版, 可持久化) ----------
@dataclass
class WeakItem:
    event_id: str
    event_content: str
    weakness_type: str           # no_source | conflict | sensitive | isolated | stale
    priority: float = 0.0
    reason: str = ""
    mention_count: int = 1
    ts: str = ""
    # 内容域: 触发该缺口的断言原句与所在素材(前端"待核实清单"要指得回原文)
    claim: str = ""
    source_eid: str = ""

    def to_dict(self) -> dict:
        return {"event_id": self.event_id, "event_content": self.event_content,
                "weakness_type": self.weakness_type, "priority": self.priority,
                "reason": self.reason, "mention_count": self.mention_count, "ts": self.ts,
                "claim": self.claim, "source_eid": self.source_eid}

    @staticmethod
    def from_dict(d: dict) -> "WeakItem":
        return WeakItem(
            event_id=d.get("event_id", ""), event_content=d.get("event_content", ""),
            weakness_type=d.get("weakness_type", "isolated"),
            priority=float(d.get("priority") or 0.0), reason=d.get("reason", ""),
            mention_count=int(d.get("mention_count") or 1), ts=d.get("ts", ""),
            claim=d.get("claim", ""), source_eid=d.get("source_eid", ""),
        )


# ---------- GraphState(纯 dict 规范, 便于快照/重放) ----------
def empty_state() -> dict:
    """规范: state = {"events": {eid: dict}, "weak": [dict], "edges": {graph: {key: dict}}}

    cites(有向 "seg>{eid}")是溯源链的物质基础: 稿件第 N 句 → 素材(design/11 §5.1)。
    """
    return {"events": {}, "weak": [],
            "edges": {"entity": {}, "temporal": {}, "causal": {}, "cites": {}}}


def state_events(state: dict) -> dict:
    return state["events"]


def state_edges(state: dict) -> dict:
    """edges = {entity: {}, temporal: {}, causal: {}}(见 empty_state 规范)。"""
    return state["edges"]


def state_weak(state: dict) -> List[dict]:
    return state["weak"]


# ---------- 提取工具(供 classifier / memory_service) ----------
def extract_techs(text: str) -> List[str]:
    """按词典抽技术栈(归一化小写)。"""
    low = norm_text(text)
    found = []
    for t in _TECH_TOKENS:
        if t in low and t not in found:
            found.append(t)
    return found


# 数字前后的量词/单位(内容域: 约 200 人 / 3 支队伍 / 5 场讲座)。
# 注意: 不收 "月"/"周" —— 内容和 "11月15日" 冲突, 会把日期读成指标。
_UNIT_WORDS = ("%", "倍", "万", "人", "名", "位", "支", "个", "件", "场", "篇", "次",
               "小时", "分钟", "天", "元", "分", "毫秒", "ms", "qps")
_UNIT_ALT = "|".join(_UNIT_WORDS)
# 带单位: 数字 + 量词(距离上限 6, 挡住 "11月15日" 这类日期)
_NUM_UNIT_RE = re.compile(rf"(?<!\d)(\d+(?:\.\d+)?)\s*([{_UNIT_ALT}]|{_UNIT_ALT})(?!\d)")
# 无单位: 数字 (距离上限 3, 避免蹭到句子里别的数字)
_NUM_BARE_RE = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)(?!\d)")
# 疑似隐私/编号数字(手机号、身份证、长编号): 不当指标, 它们由 sensitive 缺口负责
_METRIC_MAX = 1_000_000


def _metric_number(low: str, word: str):
    """在指标词的所有出现位置附近取数字, 返回 (value, unit) 或 None。

    三条硬规则(都是被真实语料逼出来的):
      1. **取最近的**: 一句里常有多个数字("预计约200人参加, 报名队伍共12支"), 只认
         第一处会把 12 支当成参加人数, 凭空造出一个假矛盾;
      2. **带单位的优先**(距离 ≤6), 无单位的要贴得很近(距离 ≤3) —— 否则
         "报名截止11月15日" 会把 15 当成报名人数;
      3. **日期与长编号不是指标**: "11月20日" 归 Period.parse, 手机号归 sensitive。
    """
    best = None
    for idx in _all_indices(low, word):
        # 带单位优先; 无单位的要**紧贴**指标词(≤1), 否则 "下午3点" 会被读成场次
        for pattern, limit in ((_NUM_UNIT_RE, 6), (_NUM_BARE_RE, 1)):
            found = None
            for m in pattern.finditer(low):
                s, e = m.start(), m.end()
                if s >= idx:
                    dist = s - (idx + len(word))
                elif e <= idx:
                    dist = idx - e
                else:
                    dist = 0                       # 重叠: 数字紧跟指标词
                if dist < 0 or dist > limit:
                    continue
                if found is None or abs(dist) < abs(found[0]):
                    found = (dist, m)
            if found is None:
                continue
            m = found[1]
            value = float(m.group(1))
            if value > _METRIC_MAX:
                continue
            if _looks_like_date(low, m.end(), pattern is _NUM_BARE_RE):
                continue
            unit = m.group(2) if (m.lastindex or 0) >= 2 and m.group(2) else None
            if unit is None and word in ("参加", "参与", "到场", "人数", "观众"):
                unit = word
            if best is None or abs(found[0]) < abs(best[0]):
                best = (found[0], value, unit)
            break          # 带单位的优先: 本句已有带单位命中, 不再退到裸数字
    return (best[1], best[2]) if best else None


def _looks_like_date(low: str, end: int, bare: bool) -> bool:
    """数字后面紧跟 日/号/月 → 它是日期, 不是指标。"""
    tail = low[end:end + 1]
    if tail in ("日", "号"):
        return True
    return bare and tail == "月"


def _all_indices(hay: str, needle: str) -> List[int]:
    out, start = [], 0
    while True:
        i = hay.find(needle, start)
        if i < 0:
            return out
        out.append(i)
        start = i + 1


def extract_metrics(text: str) -> List[Metric]:
    """极简量化抽取(内容域扩展): 识别 指标词+数字(+单位), 同一指标键只取首个。

    两种语序都收:
      - 指标词在数字前: "到场约200人" / "准确率提升15%"
      - 数字带量词在后: "参加人数 183 人" / "共 5 场讲座"
    非空结果全部进 claims 供事实核验比对(design/11 §5.1)。
    """
    out: List[Metric] = []
    low = norm_text(text)
    for word, key in _METRIC_KEYS.items():
        if any(m.key == key for m in out):
            continue
        if word not in low:
            continue
        got = _metric_number(low, word)
        if got is None:
            continue
        value, unit = got
        kind = "delta" if any(k in low for k in _DELTA_HINTS) else "absolute"
        out.append(Metric(key=key, value=value, unit=unit,
                          kind=kind, raw=text.strip()))
    return out


def guess_material_type(text: str, subject: str, techs: List[str], period) -> str:
    """按关键词推断素材类型(design/11 §4.1; 零 LLM 启发式)。

    优先级: 成果 > 通知 > 人物 > 组织 > 活动 > 背景。
    "背景"是兜底: 沿革/章程/往届这类没有时间指向的归档素材。
    """
    low = norm_text(text)
    # 归档背景优先: "章程/沿革/往届"这类词一出现, 主体往往是社团/院系(ORG),
    # 但素材性质是背景资料, 必须排在 ORG 之前判(design/11 §4.1)。
    if any(k in low for k in ("沿革", "章程", "往届情况", "历史情况", "背景资料",
                              "简介材料", "目录归档")):
        return BACKGROUND
    if any(k in low for k in ("获奖", "荣获", "一等奖", "二等奖", "三等奖", "立项",
                              "专利", "发表", "论文", "冠军", "亚军", "季军", "夺冠")):
        return ACHIEVEMENT
    if any(k in low for k in ("通知", "公告", "报名", "截止", "调整", "延期", "取消",
                              "地点变更", "时间变更", "须知")):
        return NOTICE
    if any(k in low for k in ("社长", "部长", "主席", "队长", "指导老师", "嘉宾",
                              "采访", "同学表示", "负责人说")):
        return PERSON
    if any(k in low for k in ("社团", "学生会", "团委", "院系", "学院", "协会", "联合会")):
        return ORG
    if any(k in low for k in ("活动", "讲座", "比赛", "竞赛", "纳新", "招新", "志愿",
                              "晚会", "沙龙", "分享会", "培训", "开放日", "路演", "展会")):
        return ACTIVITY
    return BACKGROUND


def guess_event_type(text: str, subject: str, techs: List[str], period) -> str:
    """按关键词粗略猜测事件类型(v1 启发式)。"""
    low = norm_text(text)
    if any(k in low for k in ("竞赛", "比赛", "大赛", "建模")):
        return COMPETITION
    if any(k in low for k in ("论文", "专利", "顶会", "期刊")):
        return ACHIEVEMENT
    if any(k in low for k in ("大学", "学院", "本科", "硕士", "博士", "专业", "gpa", "绩点", "学位")):
        return EDUCATION
    if any(k in low for k in ("实习", "工作", "任职", "部门", "岗位")):
        return EXPERIENCE
    if techs and len(subject) <= 8 and not any(k in low for k in ("系统", "平台", "项目", "网站", "参与", "负责", "开发", "实现", "做了", "搭建")):
        return SKILL
    if any(k in low for k in ("系统", "平台", "项目", "网站", "应用", "参与", "负责", "开发", "实现", "搭建", "设计了")):
        return PROJECT
    return EXPERIENCE
