# coding: utf-8
"""components/content_tools.py — 采编复盘团队的领域工具(design/11 §3.2 / §9 T3.2)。

内容域工具共五个, 全部是只读 + 一个入选题库的写操作:

| 工具 | 语义 |
|---|---|
| `read_material_library` | 读素材库(带来源通道与权威度档位) |
| `read_check_list` | 读《待核实清单》(事实核查员的主武器) |
| `render_draft` | 渲染当前稿件版本 |
| `retrieve_materials` | 按语义检索素材 |
| `add_topic_bank` | 把选题沉淀进选题库 |

没有"用户画像"类工具: 采编台不需要给编辑画像, 只需要素材、清单与当前稿件。

全部确定性、零静默 LLM; 复盘会话内读取的是该复盘节点**当时**的素材库快照与稿件版本
(design/11 §9 T3.2: 团队成员能读到当时快照素材与稿件)。

provider 名: `review_domain_read` / `review_domain_full` / `review_domain_objective` /
`review_domain_question`(阵容与 UI 都由 EXPERTS 驱动, 不新增概念)。

**openjiuwen 可选**: 没装 LLM 运行时时本模块仍可 import(工具照常定义, 只是不往团队注册),
`TEAM_TOOLS_AVAILABLE=False`。团队路线本来就用不上 —— 那种机器上走确定性路线。
"""
from __future__ import annotations

try:                                   # 装了 openjiuwen 才能把工具注册进团队
    from openjiuwen.core.foundation.tool import tool
    from openjiuwen.harness.schema.deep_agent_spec import register_tool_provider
    TEAM_TOOLS_AVAILABLE = True
except ImportError:                    # 只跑确定性链路: 装饰器退化成空操作
    TEAM_TOOLS_AVAILABLE = False

    def tool(**_kwargs):               # noqa: N802 - 与 openjiuwen 同名, 供装饰器语法
        def deco(fn):
            return fn
        return deco

    def register_tool_provider(*_args, **_kwargs):
        return None


def _current_snapshot(club_id: str):
    """若调用发生在采编复盘会话内 → 返回该复盘绑定的当时快照; 否则 None(走在线数据)。"""
    try:
        from openjiuwen.agent_teams.context import get_session_id
        sid = get_session_id()
    except Exception:  # noqa: BLE001
        return None
    if not sid:
        return None
    from components.review_runtime import get_review_runtime
    ses = get_review_runtime().get_by_session(sid)
    snap = getattr(ses, "snapshot", None) if ses is not None else None
    if not snap or snap.get("error"):
        return None
    return snap


def _embedder():
    """素材语义检索用的本地向量器(EMBEDDING_OFF=1 时返回 None, 检索退化为词面匹配)。"""
    try:
        from components.embedding import create_embedding_from_env
        return create_embedding_from_env()
    except Exception:  # noqa: BLE001
        return None


def _content():
    from content.content_runtime import get_content_runtime
    return get_content_runtime()


def _materials(club_id: str, snap=None) -> list:
    """素材列表: 复盘会话内 = 当时快照; 否则在线素材库。"""
    if snap is not None:
        return list(snap.get("materials") or [])
    return _content().materials(club_id)


@tool(
    name="read_material_library",
    description="读取社团的素材库(只读)。每条素材带 类型/来源通道/权威度/时间/是否含隐私。"
                "采编复盘会话内读取的是该复盘节点**当时**的素材库快照。"
                "核对稿件里的事实依据时用这个工具。offset/limit 用于翻页。",
    stateless=True,
)
def read_material_library(club_id: str, hint: str = "", limit: int = 20,
                          offset: int = 0) -> dict:
    snap = _current_snapshot(club_id)
    mats = _materials(club_id, snap)
    if hint:
        h = hint.strip()
        mats = [m for m in mats
                if h in (m.get("subject") or "") or h in (m.get("content") or "")]
    total = len(mats)
    start = max(0, int(offset or 0))
    window = mats[start:start + max(1, int(limit or 20))]
    slim = [{
        "eid": m.get("eid"), "type": m.get("type"), "subject": m.get("subject"),
        "content": m.get("content"), "source": m.get("source"),
        "authority": m.get("authority"), "verified": bool(m.get("verified")),
        "sensitive": bool(m.get("sensitive")), "period": m.get("period"),
    } for m in window]
    return {"count": total, "returned": len(slim), "offset": start,
            "materials": slim, "from_snapshot": snap is not None}


@tool(
    name="read_check_list",
    description="读取《待核实清单》(只读, 零 LLM): 稿件里每条站不住的断言 —— 无据/矛盾/孤证/"
                "过期/涉隐私 —— 带上原句、来源素材 eid 与原因。事实核查时先看这份清单。",
    stateless=True,
)
def read_check_list(club_id: str) -> dict:
    snap = _current_snapshot(club_id)
    if snap is not None and snap.get("check_list") is not None:
        return {"check_list": snap.get("check_list") or [],
                "summary": snap.get("gap_summary") or {}, "from_snapshot": True}
    g = _content().detect_gaps(club_id)
    return {"check_list": g.get("check_list") or [], "summary": g.get("summary") or {},
            "from_snapshot": False}


@tool(
    name="render_draft",
    description="渲染当前稿件全文(确定性模板, 不调 LLM)。采编复盘会话内渲染的是该节点**当时**"
                "的稿件版本(按素材图 seq 对齐)。需要看稿件现状时用这个工具。",
    stateless=True,
)
async def render_draft(club_id: str) -> dict:
    snap = _current_snapshot(club_id)
    if snap is not None:
        d = snap.get("draft") or {}
        return {"topic": d.get("topic") or "", "text": d.get("text") or "",
                "segments": d.get("segments") or [], "seq": d.get("seq"),
                "from_snapshot": True, "block": bool(d.get("block"))}
    rt = _content()
    cur = rt.current_draft(club_id)
    from content.content_versions import draft_to_text
    return {"topic": cur.get("topic") or "", "seq": cur.get("seq"),
            "text": cur.get("text") or draft_to_text(cur),
            "segments": cur.get("segments") or [], "block": bool(cur.get("block")),
            "from_snapshot": False}


@tool(
    name="retrieve_materials",
    description="按语义检索素材库(本地向量, 不调云端)。采编复盘会话内检索当时素材库快照。"
                "给 query 一句话, 返回最相关的 top_k 条素材(带权威度)。",
    stateless=True,
)
async def retrieve_materials(club_id: str, query: str, top_k: int = 3) -> dict:
    from components import knowledge_store
    from graphstore.types import norm_text

    snap = _current_snapshot(club_id)
    mats = _materials(club_id, snap)
    if not mats:
        return {"query": query, "results": [], "from_snapshot": snap is not None}
    emb = _embedder()
    vecs = None
    if emb is not None and emb.available():
        try:
            vecs = await emb.embed_documents([norm_text(m.get("content") or "") for m in mats])
        except Exception:  # noqa: BLE001
            vecs = None
    docs = [{"subject": m.get("subject"), "content": (m.get("content") or "")[:160],
             "eid": m.get("eid"), "authority": m.get("authority")} for m in mats]
    results = await knowledge_store.retrieve(query, emb, vecs, docs, int(top_k or 3))
    return {"query": query, "results": results, "from_snapshot": snap is not None}


@tool(
    name="add_topic_bank",
    description="把一个选题永久存入选题库(按主题+类型去重, 零 LLM), 供后续采编轮次轮询复用。"
                "你产出的选题想要沉淀下来时调用。",
    stateless=True,
)
def add_topic_bank(club_id: str, subject: str, kind: str = "no_draft",
                   reason: str = "") -> dict:
    s = (subject or "").strip()
    if not s:
        return {"added": False, "error": "选题主题为空"}
    ts = _content().topics_for(club_id)
    got = ts.add({"kind": kind or "no_draft", "subject": s, "reason": reason,
                  "source": "review_expert", "score": 1.0, "gap_count": 1,
                  "authority_mean": 0.5})
    return {"added": bool(got.get("added")), "subject": s[:60], "id": got.get("id")}


def _build_read_tools(params, context):
    return [read_material_library, read_check_list, render_draft, retrieve_materials]


def _build_full_tools(params, context):
    # 需要写回的角色(目前只有"要往选题库沉淀"的那类, 见 _build_topic_tools);
    # 内容域没有"给编辑画像"这种写操作, 所以 full 与 read 同集。
    return [read_material_library, read_check_list, render_draft, retrieve_materials,
            add_topic_bank]


def _build_objective_tools(params, context):
    # 中立角色(事实核查员/合规风险审查): 无表达偏好, 只按素材事实判定
    return [read_material_library, read_check_list, render_draft, retrieve_materials]


def _build_topic_tools(params, context):
    # 需要把选题沉淀下来的角色: 中立只读 + 入选题库
    return [read_material_library, read_check_list, render_draft, retrieve_materials,
            add_topic_bank]


register_tool_provider("content_domain_read", _build_read_tools)
register_tool_provider("content_domain_full", _build_full_tools)
register_tool_provider("content_domain_objective", _build_objective_tools)
register_tool_provider("content_domain_topic", _build_topic_tools)


__all__ = ["register_tool_provider"]
