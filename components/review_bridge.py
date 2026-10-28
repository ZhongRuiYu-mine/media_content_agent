# coding: utf-8
"""components/review_bridge.py — 采编复盘与采编工作台的共享桥(design/11 §3.2)。

两件事:

1. `content_state()`: 只读暴露采编在线状态(素材图 head / 稿件版本 / 是否有阻断项),
   供输入区三态与复盘菜单使用;
2. `snapshot()`: 用素材图回滚技术(`state_at`: 快照 + 正序重放)物化某复盘节点的
   "当时状态" —— 当时的素材库 / 缺口 / 稿件, 全部只读、零 LLM, 不动在线数据。

版本号口径与 design/11 §3.1 一致: **稿件版本号 = 素材图 seq**。所以"复盘这一次成稿"
与"素材图回到那一刻"是同一个坐标, 不需要第二套时间轴。
"""
from __future__ import annotations

from typing import Dict, List, Optional


def _safe_sid(club_id: str) -> str:
    return "".join(c for c in (club_id or "") if c.isalnum() or c in "-_") or "default"


def _content_rt():
    from content.content_runtime import get_content_runtime
    return get_content_runtime()


def content_state(club_id: str) -> dict:
    """采编在线状态(只读): 素材图 head / 稿件版本 / 缺口阻断 / 工作流进度。"""
    sid = _safe_sid(club_id)
    rt = _content_rt()
    st = rt.store_for(sid)
    draft = rt.current_draft(sid)
    state = st.latest()
    return {
        "club_id": sid,
        "head_seq": st.head_seq,
        "materials_count": len(state.get("events", {})),
        "cites": len(state.get("edges", {}).get("cites", {})),
        "weak": len(state.get("weak", [])),
        "draft": {"topic": draft.get("topic"), "seq": draft.get("seq"),
                  "segments": len(draft.get("segments") or []),
                  "block": bool(draft.get("block"))},
        "has_draft": bool(draft.get("segments")),
    }


def materials_payload_from_state(state: dict) -> List[dict]:
    """从任意历史 state(events dict)构建素材列表(按录入序 = 时间线)。"""
    evs = (state or {}).get("events", {}) or {}
    rows = [dict(ev) for ev in evs.values()]
    rows.sort(key=lambda x: (x.get("created_seq") or 0))
    return rows


def weak_payload_from_state(state: dict) -> List[dict]:
    weak = list((state or {}).get("weak", []) or [])
    weak.sort(key=lambda x: x.get("priority", 0), reverse=True)
    return weak


def snapshot(club_id: str, node_id: str = "root") -> dict:
    """物化某复盘节点当时状态(只读, 不动在线数据)。

    节点类型: **成稿节点**(`draft:seq`)与 **root**(工作台默认入口)。两种都物化成
    当时的素材库 + 缺口 + 稿件 —— 这一稿之所以长成这样, 是那一刻的素材决定的。
    """
    sid = _safe_sid(club_id)
    node = _get_node(sid, node_id)
    if node is None:
        return {"student_id": sid, "club_id": sid, "node": None,
                "error": f"节点不存在: {node_id}"}
    return _content_snapshot(sid, node)


def _content_snapshot(sid: str, node: dict) -> dict:
    rt = _content_rt()
    st = rt.store_for(sid)
    seq = node.get("head_seq") if node.get("kind") == "draft" else None
    if not isinstance(seq, int) or seq < 0:
        seq = st.head_seq
    try:
        state = st.state_at(int(seq))
    except ValueError as e:
        return {"club_id": sid, "student_id": sid, "node": node, "seq": int(seq),
                "error": f"该时间点(seq={seq})已被在线回滚重写: {e}"}

    materials = materials_payload_from_state(state)
    weak = weak_payload_from_state(state)

    from content.content_versions import get_any_version, versions
    ver = get_any_version(sid, int(seq))
    if ver is not None:
        draft = {"topic": ver.get("topic"), "segments": ver.get("segments") or [],
                 "text": ver.get("text") or "", "seq": int(seq),
                 "block": bool(ver.get("block")), "from_snapshot": True}
    else:
        draft = {"topic": node.get("topic") or "", "segments": node.get("segments") or [],
                 "text": "", "seq": int(seq), "block": False, "from_snapshot": False}

    check_list: List[dict] = []
    gap_summary: Dict = {}
    if materials:
        try:
            from content.fact_verifier import check_list as _cl
            from content.fact_verifier import verify_draft
            res = verify_draft(draft, materials)
            check_list = _cl(res["gaps"])
            gap_summary = res["summary"]
        except Exception as e:  # noqa: BLE001
            gap_summary = {"error": str(e)}

    return {
        "club_id": sid,
        "student_id": sid,
        "node": node,
        "seq": int(seq),
        "domain": "content",
        "materials": materials,
        "materials_count": len(materials),
        "weak": weak,
        "check_list": check_list,
        "gap_summary": gap_summary,
        "draft": draft,
        "versions": versions(sid),
    }


def _get_node(club_id: str, node_id: str) -> Optional[dict]:
    from components import review_graph
    node = review_graph.get_node(club_id, node_id)
    if node is not None:
        return node
    # draft:<seq> 形式: 直接按稿件版本定位
    if str(node_id).startswith("draft:"):
        try:
            seq = int(str(node_id).split(":", 1)[1])
        except (ValueError, IndexError):
            return None
        from content.content_versions import get_any_version
        ver = get_any_version(club_id, seq)
        if ver is None:
            return None
        return {"node_id": node_id, "kind": "draft", "head_seq": seq,
                "topic": ver.get("topic"), "label": f"成稿 seq={seq}"}
    return None


def snapshot_summary(snap: dict, limit: int = 12) -> str:
    """把快照压成给复盘团队开场的一段上下文摘要(确定性, 零 LLM)。"""
    if not snap or snap.get("error"):
        return "(当时快照不可用)"
    mats = snap.get("materials") or []
    lines = [f"当时素材库共 {snap.get('materials_count', len(mats))} 条素材, "
             f"素材图 seq={snap.get('seq')}"]
    subs = [f"{m.get('subject')}({m.get('source')}/{m.get('authority')})"
            for m in mats if m.get("subject")]
    if subs:
        lines.append("素材: " + "、".join(subs[:limit]))
    d = snap.get("draft") or {}
    if d.get("text") or d.get("segments"):
        lines.append(f"当时稿件(seq={d.get('seq')})共 {len(d.get('segments') or [])} 段, "
                     f"阻断={'是' if d.get('block') else '否'}; 可用 render_draft 看全文")
    cl = snap.get("check_list") or []
    if cl:
        lines.append("待核实清单: " + "、".join(
            f"{c.get('label')}:{(c.get('assertion') or '')[:16]}" for c in cl[:6]))
    return "\n".join(lines)


__all__ = ["content_state", "snapshot", "snapshot_summary",
           "materials_payload_from_state", "weak_payload_from_state"]
