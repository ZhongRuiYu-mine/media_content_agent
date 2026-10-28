# coding: utf-8
"""components/review_graph.py — 采编复盘节点图(design/11 §3.2: 节点 = 成稿节点)。

从既有落盘确定性派生一张线性时间轴(**零新增采集、零 LLM**):

    root(素材库) → 成稿 1 → 成稿 2 → ... → 最新一次成稿

节点来源: `data/reports/{club}.jsonl` 里内容域步骤写下的成稿行(`topic_draft`,
含 topic/segments/head_seq); 素材图 seq 由 `data/graphs/{club}/head.json` 提供。
"素材图回到那一刻"与"复盘这一次成稿"是同一个坐标, 不需要第二套时间轴。
"""
from __future__ import annotations

import json
import os
from typing import List, Optional

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_DIR = os.path.join(_BASE_DIR, "data")

# 内容域"成稿"步骤标记(content.workflow.WORKFLOW_STEPS 的第 3 步)
DRAFT_STEP = "topic_draft"


def _safe_sid(student_id: str) -> str:
    return "".join(c for c in (student_id or "") if c.isalnum() or c in "-_") or "default"


def _report_path(student_id: str) -> str:
    base = os.getenv("REPORT_DIR", os.path.join(_DATA_DIR, "reports"))
    return os.path.join(base, f"{_safe_sid(student_id)}.jsonl")


def _graph_head_seq(student_id: str) -> int:
    """素材图当前 head_seq(head.json, 缺失则 0)。"""
    p = os.path.join(os.getenv("STORAGE_BASE_DIR", os.path.join(_DATA_DIR, "graphs")),
                     _safe_sid(student_id), "head.json")
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return int(json.load(f).get("head_seq", 0))
        except (json.JSONDecodeError, OSError, ValueError):
            return 0
    return 0


def _read_report_rows(student_id: str) -> List[dict]:
    """读取报告行(按落盘顺序)。"""
    rows: List[dict] = []
    p = _report_path(student_id)
    if not os.path.exists(p):
        return rows
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if isinstance(rec, dict):
                        rows.append(rec)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return rows
    return rows


def build_graph(student_id: str) -> dict:
    """返回 {root, nodes:[...]}, nodes 按时间升序。

    每个"真出了段结构"的成稿行 = 一个**成稿节点**(kind=draft)。
    同一 step 下的选题扫描行(scan_topics)没有段结构, 不单独成节点。
    """
    rows = _read_report_rows(student_id)
    root = {
        "node_id": "root",
        "kind": "root",
        "ord": 0,
        "label": "素材库",
        "head_seq": _graph_head_seq(student_id),
        "materials_count": None,
    }
    nodes: List[dict] = []
    draft_i = 0
    for rec in rows:
        segs = rec.get("segments") or []
        if not segs:
            continue
        draft_i += 1
        nodes.append({
            "node_id": f"seg_{draft_i}",
            "kind": "draft",
            "ord": len(nodes) + 1,
            "topic": rec.get("topic") or "",
            "segments": segs,
            "label": f"第 {draft_i} 次成稿: {rec.get('topic') or '未命名'}",
            "head_seq": rec.get("head_seq"),
            "ts": rec.get("ts") or "",
            "step": rec.get("step") or "",
            "phase": rec.get("phase") or "",
        })
    return {"student_id": _safe_sid(student_id), "root": root, "nodes": nodes}


def get_node(student_id: str, node_id: str) -> Optional[dict]:
    """取单个节点(root / seg_N 成稿节点 / draft:seq)。"""
    g = build_graph(student_id)
    if node_id == "root" or node_id == g["root"]["node_id"]:
        return g["root"]
    if str(node_id).startswith("draft:"):
        try:
            seq = int(str(node_id).split(":", 1)[1])
        except (ValueError, IndexError):
            return None
        for n in g["nodes"]:
            if n.get("kind") == "draft" and n.get("head_seq") == seq:
                return n
        return None
    for n in g["nodes"]:
        if n["node_id"] == node_id:
            return n
    return None


def draft_node(student_id: str, seq: int, topic: str = "",
               segments: Optional[list] = None) -> dict:
    """按 seq 构造一个成稿节点(供内容域直接创建复盘用)。"""
    return {
        "node_id": f"draft:{int(seq)}",
        "kind": "draft",
        "head_seq": int(seq),
        "topic": topic,
        "segments": segments or [],
        "label": f"成稿 seq={int(seq)}: {topic or '未命名'}",
    }


__all__ = ["build_graph", "get_node", "draft_node", "DRAFT_STEP"]
