# coding: utf-8
"""graphstore/file_graph.py — 用户事件图本地持久化(design/02 + 05 §2 落地)。

机制:
- 每学生目录: log.jsonl(只追加 op) + head.json + snapshots/seq_XXXXXX.json;
- op 由 commit 分配 seq(单调, 从 1 起); apply(op) 是纯函数, 事件 eid=evt_{seq};
- 回滚到 S: 载入最新快照(seq<=S) → 依序重放 (snap_seq+1..S) → **截断** log 中 seq>S 的行
  (回滚即"回到过去并从此重写未来", 等价于撤销; 被截断分支如需保留请先手动 snapshot);
- 自动边(仅 entity, 纯文本规则): subject 相同 / techs 交集>0, ADD/UPDATE 时整体重算 ——
  不依赖 embedding(纯确定性, 见 design/02 §4 采纳修订)。
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from .types import empty_state, iso_now, safe_student, state_events, state_weak

logger = logging.getLogger(__name__)

_EID_RE = re.compile(r"^evt_(\d{6})$")
# 有向边: 键 "{src}>{tgt}"。cites 必须在此(稿件句 → 素材, 方向本身就是信息)
_DIRECTED = ("temporal", "causal", "cites")


def _entity_edge_key(a: str, b: str) -> str:
    return "|".join(sorted((a, b)))


class FileGraphStore:
    """单学生图存储(log + 快照 + head + 时间点回滚)。"""

    def __init__(self, base_dir: str, student_id: str,
                 snapshot_every: int = 20, now=None):
        sid = safe_student(student_id)
        self.student_id = sid
        self.snapshot_every = max(1, int(snapshot_every))
        self._now = now or iso_now
        self._dir = os.path.join(base_dir, sid)
        os.makedirs(os.path.join(self._dir, "snapshots"), exist_ok=True)
        self._log_path = os.path.join(self._dir, "log.jsonl")
        self._head_path = os.path.join(self._dir, "head.json")
        self._rollback_log = os.path.join(self._dir, "rollback.log.jsonl")

        self.state: dict = empty_state()
        self.head_seq: int = 0
        self._loaded = False
        self._ensure_edge_graphs()

    def _ensure_edge_graphs(self) -> None:
        """补齐 edges 子图键(design/11 §4.2)。

        cites(溯源链)是新增子图; 老日志/老快照里没有它, 重放与读取都必须能容错。
        entity/temporal 由规则重算, causal/cites 只由显式 ADD_EDGE 维护, 不参与重算。
        """
        edges = self.state.setdefault("edges", {})
        for g in ("entity", "temporal", "causal", "cites"):
            edges.setdefault(g, {})

    # ---------- 路径/读写 ----------
    def _snapshot_path(self, seq: int) -> str:
        return os.path.join(self._dir, "snapshots", f"seq_{seq:06d}.json")

    def _atomic_write(self, path: str, data) -> None:
        # 与 commit 同理: 目录可能被外部删掉, 写之前补一次
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def _read_head(self) -> int:
        if not os.path.exists(self._head_path):
            return 0
        try:
            with open(self._head_path, "r", encoding="utf-8") as f:
                return int(json.load(f).get("head_seq", 0))
        except (OSError, ValueError, json.JSONDecodeError):
            return 0

    def _read_log_lines(self) -> List[dict]:
        """读取 log 全部行; 半行(无换行结尾的最后一行)视为未提交, 截断忽略。"""
        if not os.path.exists(self._log_path):
            return []
        ops: List[dict] = []
        with open(self._log_path, "r", encoding="utf-8") as f:
            raw = f.read()
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ops.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("graphstore: 丢弃损坏 log 行")
        return ops

    def _list_snapshots(self) -> List[int]:
        d = os.path.join(self._dir, "snapshots")
        out = []
        if os.path.isdir(d):
            for name in os.listdir(d):
                m = re.match(r"^seq_(\d{6})\.json$", name)
                if m:
                    out.append(int(m.group(1)))
        return sorted(out)

    # ---------- 纯 apply(供 commit 与回滚重放共用) ----------
    @staticmethod
    def _recompute_rule_temporal(edges_state: Dict[str, dict], events: Dict[str, dict]) -> Dict[str, dict]:
        """temporal 规则边 = 纯函数(events)(design/07 §4.1)。

        保留非 rule 来源的显式边(LLM/用户确认的 ADD_EDGE), 用规则推导边覆盖 rule 来源部分。
        """
        from .rules import derive_rule_temporal_edges
        kept = {k: v for k, v in (edges_state.get("temporal") or {}).items()
                if v.get("source") != "rule"}
        kept.update(derive_rule_temporal_edges(events))
        return kept

    @staticmethod
    def _recompute_entity_edges(events: Dict[str, dict]) -> Dict[str, dict]:
        """entity 边 = 纯函数(events): subject 相同 / techs 有交集。"""
        edges: Dict[str, dict] = {}
        evs = list(events.values())
        for i in range(len(evs)):
            for j in range(i + 1, len(evs)):
                a, b = evs[i], evs[j]
                weight = None
                kind = None
                if a.get("subject") and a.get("subject") == b.get("subject"):
                    weight, kind = 1.0, "same_subject"
                elif set(a.get("techs") or []) & set(b.get("techs") or []):
                    weight, kind = 0.7, "shared_tech"
                if weight is not None:
                    key = _entity_edge_key(a["eid"], b["eid"])
                    edges[key] = {"src": a["eid"], "tgt": b["eid"],
                                  "kind": kind, "weight": weight}
        return edges

    @staticmethod
    def _normalize_record(rec: dict) -> dict:
        """把事件记录补齐到规范形状(design/11 §4.1)。

        必须覆盖三条路径: 新写入 / 老日志重放 / 快照恢复。老数据缺 authority 时
        按来源通道补(权威度是策略值, 可从 source 确定性推出, 不依赖 LLM 也不存快照外信息)。
        """
        from .types import authority_of

        rec.setdefault("keywords", [])
        rec.setdefault("techs", [])
        rec.setdefault("metrics", [])
        rec.setdefault("claims", [])
        if rec.get("authority") is None:
            rec["authority"] = authority_of(rec.get("source", "user"))
        rec["verified"] = bool(rec.get("verified", False))
        rec["sensitive"] = bool(rec.get("sensitive", False))
        return rec

    def _apply_op(self, op: dict) -> None:
        """应用一条 op 到 self.state(纯函数: 不依赖外部状态/时间/随机)。"""
        kind = op.get("op")
        payload = op.get("payload") or {}
        events = state_events(self.state)
        weak = state_weak(self.state)
        seq = op.get("seq")

        if kind == "ADD_EVENT":
            rec = dict(payload.get("record") or {})
            rec["eid"] = f"evt_{seq:06d}"
            rec["created_seq"] = seq
            rec["updated_seq"] = seq
            rec.setdefault("mention_count", 0)
            rec.setdefault("ts", op.get("ts", ""))
            self._normalize_record(rec)
            events[rec["eid"]] = rec
            self.state["edges"]["entity"] = self._recompute_entity_edges(events)
            self.state["edges"]["temporal"] = self._recompute_rule_temporal(self.state["edges"], events)
            return

        if kind == "UPDATE_EVENT":
            eid = payload.get("eid")
            ev = events.get(eid)
            if ev is None:
                logger.warning("UPDATE_EVENT: 事件不存在 %s", eid)
                return
            new_fields = payload.get("new_fields") or {}
            for k, v in new_fields.items():
                if isinstance(v, str) and v.startswith("$inc:"):
                    ev[k] = int(ev.get(k, 0)) + int(v[len("$inc:"):])
                else:
                    ev[k] = v
            ev["updated_seq"] = seq
            # subject/techs/period 变化 → 重算 entity/temporal 规则边(整图纯函数)
            self.state["edges"]["entity"] = self._recompute_entity_edges(events)
            self.state["edges"]["temporal"] = self._recompute_rule_temporal(self.state["edges"], events)
            return

        if kind == "REMOVE_EVENT":
            eid = payload.get("eid")
            events.pop(eid, None)
            self.state["weak"] = [w for w in weak if w.get("event_id") != eid]
            self.state["edges"]["entity"] = self._recompute_entity_edges(events)
            self.state["edges"]["temporal"] = self._recompute_rule_temporal(self.state["edges"], events)
            return

        if kind == "ADD_EDGE" or kind == "REMOVE_EDGE":
            graph = payload.get("graph")
            if graph in self.state["edges"]:
                e = self.state["edges"][graph]
                key = (f"{payload['src']}>{payload['tgt']}" if graph in _DIRECTED
                       else _entity_edge_key(payload["src"], payload["tgt"]))
                if kind == "ADD_EDGE":
                    e[key] = {"src": payload["src"], "tgt": payload["tgt"],
                              "kind": payload.get("kind"), "weight": payload.get("weight", 0.5)}
                else:
                    e.pop(key, None)
            return

        if kind == "ADD_WEAK":
            eid = payload.get("event_id")
            ev = events.get(eid)
            content = ev.get("content", "") if ev else payload.get("event_content", "")
            wtype = payload.get("weakness_type", "isolated")
            for w in weak:
                if w.get("event_id") == eid and w.get("weakness_type") == wtype:
                    w["mention_count"] = int(w.get("mention_count", 1)) + 1
                    w["priority"] = max(float(w.get("priority") or 0.0),
                                        float(payload.get("priority") or 0.0))
                    if payload.get("claim") and not w.get("claim"):
                        w["claim"] = payload["claim"]
                    if not w.get("source_eid"):
                        w["source_eid"] = payload.get("source_eid") or (eid or "")
                    return
            weak.append({
                "event_id": eid, "event_content": content,
                "weakness_type": wtype,
                "priority": float(payload.get("priority") or 0.0),
                "reason": payload.get("reason", ""), "mention_count": 1,
                "ts": op.get("ts", ""),
                # 内容域: 触发缺口的断言原句 + 所在素材(待核实清单要指得回原文)
                "claim": payload.get("claim", ""),
                "source_eid": payload.get("source_eid", "") or (eid or ""),
            })
            return

        if kind == "REMOVE_WEAK":
            eid = payload.get("event_id")
            wtype = payload.get("weakness_type")
            self.state["weak"] = [
                w for w in weak
                if not (w.get("event_id") == eid and w.get("weakness_type") == wtype)
            ]
            return

        if kind == "RESUME_REF":
            # 阶段3 稿件版本锚点, v1 对图状态无副作用
            return

        logger.warning("graphstore: 未知 op %s", kind)

    # ---------- 快照 ----------
    def _save_snapshot(self, seq: int, reason: str = "") -> str:
        data = {
            "seq": seq, "head": seq, "reason": reason,
            "state": copy.deepcopy(self.state),
        }
        self._atomic_write(self._snapshot_path(seq), data)
        return self._snapshot_path(seq)

    def _load_snapshot(self, seq: int) -> Optional[dict]:
        p = self._snapshot_path(seq)
        if not os.path.exists(p):
            return None
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("state")

    # ---------- 生命周期 ----------
    def open(self) -> "FileGraphStore":
        """载入到 head(幂等): 最新快照(<=head) + 正序重放至 head。"""
        head = self._read_head()
        state = empty_state()
        snaps = [s for s in self._list_snapshots() if s <= head]
        if snaps:
            st = self._load_snapshot(snaps[-1])
            if st is not None:
                state = st
        # 载入中间态到临时对象再 apply(避免污染 self.state)
        tmp_self = object.__new__(FileGraphStore)
        tmp_self.state = state
        tmp_self._ensure_edge_graphs()
        base_seq = snaps[-1] if snaps else 0
        for op in self._read_log_lines():
            s = int(op.get("seq", 0))
            if s <= base_seq or s > head:
                continue
            tmp_self._apply_op(op)
        self.state = copy.deepcopy(tmp_self.state)
        self.head_seq = head
        self._loaded = True
        self._ensure_edge_graphs()
        logger.info("graphstore: %s loaded head=%d events=%d", self.student_id, head,
                    len(state_events(self.state)))
        return self

    # ---------- 对外 API(与 design/05 一致) ----------
    def commit(self, op: str, payload: dict, session_id: str = "", ts: Optional[str] = None) -> dict:
        """提交一条 op: 分配 seq=head+1, apply, 落盘 log+head, 周期快照。"""
        if not self._loaded:
            self.open()
        seq = self.head_seq + 1
        ts = ts or self._now()
        entry = {"seq": seq, "ts": ts, "session_id": session_id, "op": op, "payload": payload}
        # 先 apply 内存(失败则不落盘)
        self._apply_op(entry)
        # 落盘 log(追加)与 head(原子)
        # 目录可能被外部删掉(换数据目录、容器挂载、手工清理), 追加前补一次 —— 否则
        # 整个成稿/入库会因为 FileNotFoundError 直接失败, 而内存里其实已经改好了。
        os.makedirs(self._dir, exist_ok=True)
        with open(self._log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()
        self.head_seq = seq
        self._atomic_write(self._head_path, {"head_seq": seq, "updated_at": ts})
        snap_seq = seq
        if seq % self.snapshot_every == 0:
            self._save_snapshot(seq, reason="auto")
        return {"seq": seq, "head": seq, "op": op, "events": len(state_events(self.state)),
                "weak": len(state_weak(self.state))}

    def rollback(self, to_seq: int, session_id: str = "", reason: str = "") -> dict:
        """时间点回滚到 to_seq: 快照+正序重放, 并截断 log 中 seq>to_seq 的行。"""
        if not self._loaded:
            self.open()
        to_seq = int(to_seq)
        if to_seq < 0 or to_seq > self.head_seq:
            raise ValueError(f"rollback 越界: 0<=to_seq({to_seq})<=head({self.head_seq})")
        if to_seq == self.head_seq:
            return {"seq": to_seq, "rolled_back": False}

        from_seq = self.head_seq
        state = empty_state()
        snaps = [s for s in self._list_snapshots() if s <= to_seq]
        base_seq = 0
        if snaps:
            st = self._load_snapshot(snaps[-1])
            if st is not None:
                state = st
                base_seq = snaps[-1]
        tmp = object.__new__(FileGraphStore)
        tmp.state = state
        for op in self._read_log_lines():
            s = int(op.get("seq", 0))
            if s <= base_seq or s > to_seq:
                continue
            tmp._apply_op(op)
        self.state = copy.deepcopy(tmp.state)
        self.head_seq = to_seq
        self._atomic_write(self._head_path, {"head_seq": to_seq,
                                             "updated_at": self._now()})
        # 截断未来分支(回滚 = 回到过去并重写未来)
        ops = [op for op in self._read_log_lines() if int(op.get("seq", 0)) <= to_seq]
        with open(self._log_path, "w", encoding="utf-8") as f:
            for op in ops:
                f.write(json.dumps(op, ensure_ascii=False) + "\n")
            f.flush()
        with open(self._rollback_log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": self._now(), "session_id": session_id,
                                "to_seq": to_seq, "from_seq": from_seq,
                                "reason": reason}, ensure_ascii=False) + "\n")
        logger.info("graphstore: %s rollback %d -> %d", self.student_id, from_seq, to_seq)
        return {"seq": to_seq, "rolled_back": True, "from_seq": from_seq}

    def latest(self) -> dict:
        """最新状态(events/weak/edges)深拷贝, 供只读路径。"""
        if not self._loaded:
            self.open()
        return copy.deepcopy(self.state)

    def state_at(self, seq: int) -> dict:
        """只读查看任意历史 seq 的状态(快照+正序重放, 不修改 head/log)。

        供阶段3 稿件版本联动: 按 seq 确定性重渲染稿件, 或核对回滚落点。
        """
        if not self._loaded:
            self.open()
        seq = int(seq)
        if seq < 0 or seq > self.head_seq:
            raise ValueError(f"state_at 越界: 0<=seq({seq})<=head({self.head_seq})")
        state = empty_state()
        snaps = [s for s in self._list_snapshots() if s <= seq]
        base_seq = 0
        if snaps:
            st = self._load_snapshot(snaps[-1])
            if st is not None:
                state = st
                base_seq = snaps[-1]
        tmp = object.__new__(FileGraphStore)
        tmp.state = state
        for op in self._read_log_lines():
            s = int(op.get("seq", 0))
            if s <= base_seq or s > seq:
                continue
            tmp._apply_op(op)
        return copy.deepcopy(tmp.state)

    def history(self, limit: int = 50) -> List[dict]:
        """head 时间线摘要(新→旧), 供前端"历史版本"点选与回滚。"""
        if not self._loaded:
            self.open()
        items: List[dict] = []
        for op in self._read_log_lines():
            s = int(op.get("seq", 0))
            if s > self.head_seq:
                continue
            kind = op.get("op")
            p = op.get("payload") or {}
            summary = ""
            if kind == "ADD_EVENT":
                summary = (p.get("record") or {}).get("content", "")[:40]
            elif kind == "UPDATE_EVENT":
                summary = f"更新 {p.get('eid')}"
            elif kind == "REMOVE_EVENT":
                summary = f"删除 {p.get('eid')}"
            elif kind == "ADD_WEAK":
                summary = f"薄弱记忆:{(p.get('reason') or '')[:24]}"
            items.append({"seq": s, "op": kind, "ts": op.get("ts", ""),
                          "session_id": op.get("session_id", ""), "summary": summary})
        items.sort(key=lambda x: x["seq"], reverse=True)
        return items[:limit]

    def snapshot(self, reason: str = "manual") -> int:
        if not self._loaded:
            self.open()
        self._save_snapshot(self.head_seq, reason=reason)
        return self.head_seq
