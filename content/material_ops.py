# coding: utf-8
"""content/material_ops.py — 素材操作: 权威度推断 + 断言抽取 + cites 溯源边推导。

design/11 §5.1 的落地。**本模块零 LLM、零向量**(纯规则 + 2-gram 相似度), 理由:
溯源链与事实核验是本项目的核心主张, 现场断网也要能演示, 所以判定必须是确定性的。

职责:
1. `infer_authority(source, verified, sensitive)` — 来源通道 → 权威度(design/11 §4.1);
2. `extract_entities(text, known)` — 人名/职务/场地/机构四类实体(复用 types 的已知词典);
3. `segment_claims(seg, materials)` — 一个稿件段落 → 断言列表(数字/时间/实体);
4. `find_support(claim, materials)` — 断言 → 支撑素材(归一化 + 2-gram 相似度);
5. `derive_cites(...)` — 段落 × 素材 → cites 边(`"{seg}>{eid}"`, 有向)。

`cites` 边的键约定与 graphstore 完全一致(`_DIRECTED` 含 cites), 因此这些边可以直接
`store.commit("ADD_EDGE", {"graph": "cites", ...})` 落盘, 且随 seq 回滚一起消失。
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from graphstore.types import (
    Period, authority_of, extract_metrics, norm_text, subject_similarity,
)

# 相似度阈值: 沿用原分类器标定值(design/11 §5.1), 不另造一套数字
SIM_DUP = 0.90          # 判重: 断言与素材几乎同句
SIM_RELATED = 0.55      # 相关: 可作为支撑
SUBJECT_SIM_HIT = 0.40  # 主体命中: 实体出现在素材里

# ---------- 实体词典(内容域: 人名/职务/场地/机构) ----------
ROLE_WORDS = (
    "社长", "副社长", "部长", "副部长", "主席", "副主席", "队长", "副队长",
    "会长", "副会长", "秘书长", "团支书", "班长", "部长助理",
    "指导老师", "带队老师", "辅导员", "班主任", "教练", "嘉宾", "主讲人",
    "主持人", "负责人", "发起人", "评委", "校友",
)
VENUE_WORDS = (
    "报告厅", "大礼堂", "礼堂", "体育馆", "操场", "运动场", "田径场", "篮球场",
    "足球场", "羽毛球馆", "游泳馆", "大学生活动中心", "活动中心", "学生活动中心",
    "多功能厅", "会议室", "阶梯教室", "教室", "实验室", "机房", "图书馆",
    "阅览室", "广场", "中庭", "展厅", "创客空间", "路演厅", "演播厅", "音乐厅",
    "食堂", "宿舍楼", "教学楼", "实训楼", "科技楼", "综合楼", "南门", "北门",
)
ORG_SUFFIX = ("学院", "书院", "院系", "学部", "团委", "学生会", "研究生会", "社团",
              "协会", "联合会", "联盟", "中心", "工作室", "志愿队", "服务队",
              "组委会", "委员会", "基金会", "公司", "企业", "基地")
NAME_SUFFIX = ("同学", "老师", "教授", "学长", "学姐", "师弟", "师妹", "先生", "女士")
# 常见姓氏(用于"姓+称呼"型人名: 王老师 / 李同学 / 欧阳老师)
SURNAMES = (
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
    "戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳鲍史唐"
    "费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮齐康伍余元卜顾孟平黄"
    "和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴宋茅庞熊纪舒屈项祝董梁"
    "杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌"
    "霍虞万支柯昝管卢莫房裘缪干解应宗丁宣邓郁单杭洪包诸左石崔吉钮龚程"
    "嵇邢裴陆荣翁荀羊甄家封芮储靳汲邴糜松井段富巫乌焦巴弓牧隗山谷车侯"
    "宓蓬全郗班仰秋仲伊宫宁仇栾暴甘厉戎祖武符刘景詹束龙叶幸司韶郜黎蓟"
    "薄印宿白怀蒲邰从鄂索咸籍赖卓蔺屠蒙池乔阴郁胥能苍双闻莘党翟谭贡劳"
    "逄姬申扶堵冉宰郦雍璩桑桂濮牛寿通边扈燕冀郏浦尚农温别庄晏柴瞿阎充"
    "慕连茹习宦艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘匡国文寇广禄阙东"
    "欧殳沃利蔚越夔隆师巩厍聂晁勾敖融冷訾辛阚那简饶空曾毋沙乜养鞠须丰"
    "巢关蒯相查后荆红游竺权逯盖益桓公"
)
_SURNAME_SET = set(SURNAMES)
_COMPOUND_SURNAMES = ("欧阳", "司马", "上官", "诸葛", "东方", "皇甫", "尉迟", "公孙",
                      "慕容", "长孙", "宇文", "司徒", "鲜于", "独孤", "南宫", "令狐")
# 称呼+姓氏 型人名: "张同学" / "王老师" / "欧阳老师"。
# 只取"姓+称呼"这一形态, **不外扩**到后面的字: 中文人名断句("王老师" 的 王 是不是名的一部分)
# 没有可靠依据, 多取一个字就会把"李明老师"抽成两个人。称呼本身是可靠的锚。
_SURNAME_ALT = "|".join(sorted(set(SURNAMES) | set(_COMPOUND_SURNAMES),
                               key=len, reverse=True))
_ROLE_SUFFIX = "同学|老师|教授|学长|学姐|先生|女士"
_NAME_ROLE_RE = re.compile(rf"({_SURNAME_ALT})(?:{_ROLE_SUFFIX})")
_NAME_ROLE_STRIP_RE = re.compile(rf"^(.*?)(?:{_ROLE_SUFFIX})$")
# 场地的限定前缀: "第三报告厅" / "东区操场" — 取最长匹配
_VENUE_PREFIX = r"(?:第[\u4e00-\u9fff0-9]{1,3}|[东南西北]区|[东南西北]|主|老|新)?"
_ORG_RE_TAIL = "|".join(sorted(ORG_SUFFIX, key=len, reverse=True))


def infer_authority(source: str, verified: bool = False,
                    sensitive: bool = False) -> float:
    """来源通道 → 权威度(design/11 §4.1)。

    - 权威度只由来源通道决定, **写入素材记录后不再变**(它是策略值, 不是评分);
    - `verified`(用户勾选"主办方已确认")不改变 authority, 而是核验时的加权信号:
      已确认的低权威来源可以顶替"孤证"判定(见 fact_verifier.is_isolated);
    - `sensitive` 同理不改变 authority(隐私不等于可信度)。

    参数 `verified` / `sensitive` 收在这里只是为了让调用点语义完整、避免调用方
    自己拼 authority; 二者不参与数值计算。
    """
    return authority_of(source)


def authority_mean(materials: Iterable[dict]) -> float:
    """素材集合的权威度均值(design/11 §5.2 选题排序用)。空集返回 0.0。"""
    vals = [float(m.get("authority") or 0.0) for m in (materials or [])]
    if not vals:
        return 0.0
    return round(sum(vals) / len(vals), 4)


# ---------- 实体抽取 ----------
# 机构名左侧的"锚点": 它们之后才是机构名起点("由计算机学院" → "计算机学院")
_ORG_ANCHORS = "由在是于、，,。：:;；（）()"\
               "了使向从对和与及获被为"
_ORG_TAIL_NOISE = ("主办", "承办", "协办", "指导", "举办", "组织", "负责", "联合")
_ORG_HEAD_NOISE = ("本届", "上届", "今年", "去年", "这次", "本次", "该", "各", "全校", "全系")
_ORG_VERB_NOISE = ("举办", "承办", "协办", "负责", "组织", "参加", "参与", "评选")


def _trim_org(term: str) -> str:
    """把贪婪匹配到的机构串修成最短合法机构名。

    锚点字符(动词/标点/介词)之后才是机构名起点: "举办本届比赛由计算机学院学生科技社团"
    → "计算机学院学生科技社团"。仍带尾部/头部噪音时逐项剥掉; 过长则从左侧裁。
    """
    if not term:
        return ""
    idx = 0
    for i, ch in enumerate(term):
        if ch in _ORG_ANCHORS:
            idx = i + 1
    term = term[idx:]
    if not term.endswith(ORG_SUFFIX):
        return ""
    for tail in _ORG_TAIL_NOISE:
        if term.endswith(tail) and len(term) > len(tail):
            term = term[: -len(tail)]
    for head in _ORG_HEAD_NOISE:
        if term.startswith(head) and len(term) > len(head) + 1:
            term = term[len(head):]
    if len(term) > 14:
        term = term[-14:]
    if any(k in term for k in _ORG_VERB_NOISE):
        return ""
    return term if 2 < len(term) <= 14 else ""


def extract_entities(text: str, known: Optional[Dict[str, Sequence[str]]] = None
                     ) -> List[dict]:
    """抽取人名/职务/场地/机构四类实体(design/11 §5.1)。

    known: {"person": [...], "org": [...], "venue": [...], "role": [...]}
    素材库里已知的名字/机构是**词典来源**: 通讯录里的人名、往届推文里的机构名,
    命中即认(零 LLM)。词典之外只认高置信模式(职务词、场地词、"X同学/老师")。

    返回 [{"kind": "person|role|venue|org", "text": ..., "via": "dict|pattern"}], 按出现序。
    """
    raw = (text or "").strip()
    if not raw:
        return []
    low = norm_text(raw)
    kn = {k: [v for v in (known or {}).get(k) or [] if v] for k in
          ("person", "org", "venue", "role")}
    hits: List[Tuple[int, dict]] = []

    def add(pos: int, kind: str, txt: str, via: str) -> None:
        if not txt:
            return
        hits.append((pos, {"kind": kind, "text": txt, "via": via}))

    # 1) 词典命中(已知人名/机构/场地/职务): 位置用归一化串定位, 找不到就给末尾位
    for kind in ("person", "org", "venue", "role"):
        for term in kn[kind]:
            nt = norm_text(term)
            # 单字人名不成词("王"这种是抽取噪音, 由"姓+称呼"模式负责)
            if kind == "person" and len(term) < 2:
                continue
            if nt and nt in low:
                add(low.find(nt), kind, term, "dict")

    # 2) 职务词(词典之外): 词表命中即认
    for role in ROLE_WORDS:
        if role in low and not any(h[1]["kind"] == "role" and h[1]["text"] == role
                                   for h in hits):
            add(low.find(norm_text(role)), "role", role, "pattern")

    # 3) 场地词: 带可选限定前缀, 取最长匹配
    for venue in VENUE_WORDS:
        m = re.search(_VENUE_PREFIX + re.escape(norm_text(venue)), low)
        if m and not any(h[1]["kind"] == "venue" and venue in h[1]["text"] for h in hits):
            add(m.start(), "venue", m.group(0), "pattern")

    # 4) 机构词: 从锚点字符(动词/标点)之后起算, 取最短合法机构名,
    #    避免贪婪吞掉前面的动词("举办本届比赛由计算机学院..." → "计算机学院")
    for m in re.finditer(rf"[\u4e00-\u9fffA-Za-z0-9]{{2,16}}?(?:{_ORG_RE_TAIL})", raw):
        term = _trim_org(m.group(0))
        if term and not any(h[1]["kind"] == "org" and h[1]["text"] == term for h in hits):
            add(norm_text(raw[:m.start()]).__len__(), "org", term, "pattern")

    # 5) "姓+称呼"型人名: 姓氏白名单保证不会把"指导老师"抽成"指导"
    for m in _NAME_ROLE_RE.finditer(raw):
        surname = m.group(1)
        cand = f"{surname}{_matched_suffix(m.group(0), surname)}"
        if len(cand) < 2:
            continue
        if not any(h[1]["kind"] == "person" and h[1]["text"] == cand for h in hits):
            add(norm_text(raw[:m.start()]).__len__(), "person", cand, "pattern")

    hits.sort(key=lambda x: x[0])
    out: List[dict] = []
    seen = set()
    for _, h in hits:
        key = (h["kind"], h["text"])
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out


def _matched_suffix(full: str, surname: str) -> str:
    """取命中串里姓氏之后的那部分("王老师" + "王" → "老师")。"""
    return full[len(surname):]


def known_terms(materials: Iterable[dict]) -> Dict[str, List[str]]:
    """从素材库收集已知实体词典(person/org/venue/role)。

    人名来源: 素材的 `subject` 与 `org`/`role` 字段, 以及正文里的"X同学/X老师";
    机构来源: subject 与正文里的机构后缀词。这是"素材库越用越准"的确定性路径。
    """
    persons: List[str] = []
    orgs: List[str] = []
    venues: List[str] = []
    roles: List[str] = []
    for m in materials or []:
        subj = (m.get("subject") or "").strip()
        role = (m.get("role") or "").strip()
        org = (m.get("org") or "").strip()
        content = m.get("content") or ""
        if role:
            roles.append(role)
        if org:
            orgs.append(org)
        elif subj and subj.endswith(ORG_SUFFIX):
            orgs.append(subj)
        if subj and m.get("type") == "person" and 2 <= len(subj) <= 4:
            persons.append(subj)
        for e in extract_entities(content, None):
            # 只收"姓+称呼"这种有据的人名(单个姓氏字不成名: "张同学" 的 张 是噪音)
            if e["kind"] == "person" and len(e["text"]) >= 2:
                persons.append(e["text"])
            elif e["kind"] == "org":
                orgs.append(e["text"])
            elif e["kind"] == "venue":
                venues.append(e["text"])
    dedup = lambda xs: list(dict.fromkeys(x for x in xs if x))  # noqa: E731
    return {"person": dedup(persons), "org": dedup(orgs),
            "venue": dedup(venues), "role": dedup(roles)}


# ---------- 断言抽取 ----------
def claim_from_metric(metric, sentence: str) -> dict:
    """graphstore.types.Metric → 断言 dict(核验的输入形状)。"""
    d = metric.to_dict() if hasattr(metric, "to_dict") else dict(metric or {})
    return {"kind": "metric", "key": d.get("key") or "",
            "value": d.get("value"), "unit": d.get("unit"),
            "raw": sentence or d.get("raw") or "",
            "mention": _metric_mention(d)}


def _metric_mention(d: dict) -> str:
    """断言的"最短可读形式"(前端待核实清单要展示的那一小段)。"""
    v = d.get("value")
    if isinstance(v, float) and v == int(v):
        v = int(v)
    return f"{v}{d.get('unit') or ''}"


# 评价性表述(无据的主战场): 这类话没有可核对的数字/专名, 但正是"编出来的话"
# 最常出现的地方 —— "反响热烈""最好的一届""广泛好评"。素材里没有逐字依据就报无据。
SUBJECTIVE_MARKERS = (
    "反响热烈", "好评如潮", "广泛好评", "气氛热烈", "现场气氛热烈", "掌声不断",
    "精彩纷呈", "圆满成功", "圆满结束", "取得圆满成功", "意义重大", "意义非凡",
    "广受好评", "深受欢迎", "备受欢迎", "收获满满", "受益匪浅", "耳目一新",
    "效果显著", "成效显著", "水平很高", "质量很高", "空前盛况", "人山人海",
    "同学们都很满意", "大家都很满意", "一致好评", "点赞无数", "刷爆朋友圈",
    "参与度很高", "参与热情高涨", "同学们热情高涨", "积极性很高", "热情高涨",
)
_SUBJECTIVE_PATTERNS = (
    re.compile(r"最(?:好|佳|成功|精彩|难忘|受欢迎)的(?:一|这)?届"),
    re.compile(r"(?:是|为)(?:历史|历届)(?:上)?最[^\s,，。;；]{0,6}"),
    re.compile(r"(?:极|非常|十分|特别)(?:成功|精彩|热烈|难忘)"),
    re.compile(r"气氛(?:特别好|特别热烈|非常好|热烈)"),
    re.compile(r"(?:有意思|有意义|难忘|精彩)的(?:一|这)?届"),
)


def extract_subjective(text: str) -> List[str]:
    """抽评价性表述(逐字命中词表/模式), 按出现顺序去重。"""
    raw = text or ""
    low = norm_text(raw)
    out: List[str] = []
    for w in SUBJECTIVE_MARKERS:
        if norm_text(w) in low and w not in out:
            out.append(w)
    for rx in _SUBJECTIVE_PATTERNS:
        for m in rx.finditer(raw):
            hit = m.group(0)
            if hit and hit not in out:
                out.append(hit)
    return out


def segment_claims(seg: dict, materials: Optional[Iterable[dict]] = None) -> List[dict]:
    """一个稿件段落 → 断言列表(design/11 §5.1 的 `segment_claims`)。

    seg: {"seg_id", "text", "claims"?, ...}(引擎已抽好的断言会保留并去重)
    断言三类:
      - metric: 数字/指标(extract_metrics), 核验"素材里有没有这个数";
      - period: 日期区间(Period.parse), 核验"时间说法与通知一致";
      - entity: 人名/职务/场地/机构, 核验"这个人/这个地方在素材里出现过"。
    纯规则, 零 LLM; 同一 (kind, key, value) 只留一条。
    """
    text = (seg.get("text") or "").strip()
    out: List[dict] = []
    seen = set()

    def push(c: dict) -> None:
        key = (c.get("kind"), c.get("key"), str(c.get("value")))
        if key in seen or not c.get("key"):
            return
        seen.add(key)
        out.append(c)

    for m in extract_metrics(text):
        push(claim_from_metric(m, text))

    p = Period.parse(text)
    if p is not None and p.start:
        push({"kind": "period", "key": "period", "value": p.start,
              "unit": p.end, "raw": text, "mention": p.raw or p.start})

    known = known_terms(materials or []) if materials else None
    for e in extract_entities(text, known):
        push({"kind": "entity", "key": e["kind"], "value": e["text"],
              "unit": None, "raw": text, "mention": e["text"],
              "via": e.get("via")})

    # 评价性表述: 无据判定的主战场(数字/专名之外, 稿件最容易"自己加戏"的地方)
    for s in extract_subjective(text):
        push({"kind": "statement", "key": "subjective", "value": s,
              "unit": None, "raw": s, "mention": s})

    # 引擎预置的断言(如 LLM 抽出的)一并纳入, 仍按同一去重口径
    for c in seg.get("claims") or []:
        if isinstance(c, dict):
            push({"kind": c.get("kind") or "fact", "key": c.get("key") or "",
                  "value": c.get("value"), "unit": c.get("unit"),
                  "raw": c.get("raw") or text,
                  "mention": c.get("mention") or str(c.get("value") or "")})
    return out


# ---------- 支撑判定 ----------
def claim_in_text(claim: dict, text: str) -> Tuple[bool, float, str]:
    """断言是否被某段素材文本支撑 → (支持?, 相似度, 说明)。纯规则。"""
    t = (text or "").strip()
    if not t:
        return False, 0.0, ""
    kind = claim.get("kind")
    if kind == "metric":
        for m in extract_metrics(t):
            if m.key == claim.get("key") and _same_number(m.value, claim.get("value")):
                return True, 1.0, f"素材含同值数字 {claim.get('mention')}"
        # 同一指标键但数值不同 → 不是"无据", 是"矛盾"(交给冲突复核)
        for m in extract_metrics(t):
            if m.key == claim.get("key") and m.value is not None:
                return False, 0.5, f"同指标不同值({m.value})"
        return False, 0.0, ""
    if kind == "period":
        p = Period.parse(t)
        if p is not None and p.start and p.start == claim.get("value"):
            return True, 1.0, "素材含同一时间"
        return False, 0.0, ""
    if kind == "entity":
        v = claim.get("value") or ""
        # 实体核验用**精确出现**判定: 2-gram 覆盖会把"王老师"命中到"老师王"这类
        # 抽取噪音上——那是抽取问题, 不是核验结论, 不能报成"无据"。
        if v and norm_text(v) in norm_text(t):
            return True, 1.0, "素材出现同一专名"
        return False, 0.0, ""
    if kind == "statement":
        # 评价性表述: 只有素材里逐字出现才算有据(design/11 §4.3 no_source 的主战场)
        if _is_subjective_grounded(claim, t):
            return True, 1.0, "素材含同一表述"
        return False, 0.0, ""
    sim = subject_similarity(str(claim.get("value") or ""), t)
    return (sim >= SIM_RELATED), sim, ""


def _is_subjective_grounded(claim: dict, text: str) -> bool:
    return norm_text(str(claim.get("value") or "")) in norm_text(text)


def _same_number(a, b) -> bool:
    try:
        return abs(float(a) - float(b)) < 1e-9
    except (TypeError, ValueError):
        return False


def claim_key(claim: dict) -> str:
    """断言的稳定标识(去重/缺口合并/前端锚点都用它)。"""
    return f"{claim.get('kind')}:{claim.get('key')}:{claim.get('value')}"


def find_support(claim: dict, materials: Iterable[dict],
                 exclude_eid: Optional[str] = None) -> List[dict]:
    """断言 → 支撑素材列表(按相似度降序)。

    exclude_eid: 不把自己所属的那条素材算作"独立支撑"(孤证判定要的是**第二来源**)。
    返回 [{"eid", "authority", "source", "text", "similarity", "reason"}]
    """
    out: List[dict] = []
    for m in materials or []:
        eid = m.get("eid")
        if exclude_eid and eid == exclude_eid:
            continue
        ok, sim, why = claim_in_text(claim, m.get("content") or "")
        if ok:
            out.append({"eid": eid, "authority": float(m.get("authority") or 0.0),
                        "source": m.get("source") or "", "verified": bool(m.get("verified")),
                        "text": (m.get("content") or "")[:120],
                        "similarity": round(float(sim), 4), "reason": why})
    out.sort(key=lambda x: (-x["similarity"], -x["authority"]))
    return out


# ---------- cites 边推导 ----------
def derive_cites(seg: dict, materials: Iterable[dict],
                 min_similarity: float = SIM_RELATED) -> List[dict]:
    """段落 → cites 边(design/11 §4.2: 有向 `"{draft_seg}>{eid}"`)。

    每条边记录"这个段落的哪一条断言、被哪条素材、以多高的相似度支撑",
    这是溯源链的物质基础: 前端点击稿件里的一句话 → 用这条边跳回素材。

    返回 [{"key", "src", "tgt", "kind", "weight", "claim", "assertion", "similarity"}]
    """
    seg_id = seg.get("seg_id") or seg.get("id") or ""
    if not seg_id:
        return []
    mats = list(materials or [])
    by_eid = {m.get("eid"): m for m in mats if m.get("eid")}
    # 段落自带的 source_refs(成稿时写到段落上的来源指针)优先, 且必定成边(权重 1.0)
    edges: Dict[str, dict] = {}
    for eid in seg.get("source_refs") or []:
        if eid in by_eid:
            key = f"{seg_id}>{eid}"
            edges[key] = {"key": key, "src": seg_id, "tgt": eid, "kind": "declared",
                          "weight": 1.0, "claim": "", "assertion": "成稿时登记的来源",
                          "similarity": 1.0}
    for claim in segment_claims(seg, mats):
        for sup in find_support(claim, mats):
            if sup["similarity"] < min_similarity:
                continue
            eid = sup["eid"]
            key = f"{seg_id}>{eid}"
            prev = edges.get(key)
            if prev is not None and prev.get("similarity", 0) >= sup["similarity"]:
                continue
            edges[key] = {"key": key, "src": seg_id, "tgt": eid, "kind": claim["kind"],
                          "weight": round(float(sup["similarity"]), 4),
                          "claim": claim_key(claim),
                          "assertion": claim.get("mention") or str(claim.get("value") or ""),
                          "similarity": round(float(sup["similarity"]), 4)}
    out = list(edges.values())
    out.sort(key=lambda x: (-x["similarity"], x["tgt"] or ""))
    return out


def segment_source_refs(seg: dict, materials: Iterable[dict],
                        top_k: int = 3) -> List[str]:
    """段落 → 来源指针(成稿时写在段上, 供核验与前端悬浮条直接用)。"""
    edges = derive_cites(seg, materials)
    eids: List[str] = []
    for e in edges:
        if e["tgt"] and e["tgt"] not in eids:
            eids.append(e["tgt"])
        if len(eids) >= top_k:
            break
    return eids


__all__ = [
    "SIM_DUP", "SIM_RELATED", "SUBJECT_SIM_HIT",
    "ROLE_WORDS", "VENUE_WORDS", "ORG_SUFFIX", "SUBJECTIVE_MARKERS",
    "infer_authority", "authority_mean", "known_terms",
    "extract_entities", "extract_subjective", "segment_claims", "claim_key",
    "claim_from_metric", "find_support", "derive_cites", "segment_source_refs",
]
