# coding: utf-8
"""content/content_runtime.py — 内容域编排层(六步两相的运行时)。

把"散料 → 素材图 → 缺口 → 成稿 → 核验 → 多平台 → 审核留痕"串成一条可观测的链路,
每一步都写进稿件状态与报告行(design/11 §5.5: 运行时可观测, 答辩可逐步指认)。

分工:
- `content/material_ops.py` / `fact_verifier.py` / `topic_gap.py` / `content_engine.py`
  / `render.py` / `content_store.py` / `content_versions.py`: 纯逻辑(零 LLM 为主);
- 本模块: 编排 + 落盘 + 报告行 + 状态机推进 + "采编相/发布相"门禁;
- `server.py`: 只做 HTTP 薄壳。

门禁(design/11 §2 / §5.5): 核验有**阻断项**的稿件不许进入多平台适配。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Dict, List, Optional

from content.workflow import STEP_LABELS, WORKFLOW_PHASES, phase_of

logger = logging.getLogger(__name__)

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 工作流步骤常量(与 content/workflow.py 同一份口径)
S_INTAKE = "material_intake"
S_GAP = "gap_detect"
S_DRAFT = "topic_draft"
S_VERIFY = "fact_verify"
S_PLATFORM = "platform_adapt"
S_REVIEW = "review_log"


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ContentRuntime:
    """内容域运行时: 素材图 + 选题库 + 成稿引擎 + 平台渲染 + 稿件版本。"""

    def __init__(self, base_dir: Optional[str] = None):
        self.base_dir = base_dir or os.getenv(
            "STORAGE_BASE_DIR", os.path.join(_BASE, "data", "graphs"))
        self._stores: Dict[str, "object"] = {}
        self._topics: Dict[str, object] = {}
        self._engines: Dict[str, object] = {}
        self._last_verify: Dict[str, dict] = {}

    # ---------- 素材图(FileGraphStore: seq 日志 + 快照 + 回滚, 见 design/11 §3.1) ----------
    def store_for(self, club_id: str):
        sid = _safe(club_id)
        st = self._stores.get(sid)
        if st is None:
            from graphstore import FileGraphStore
            st = FileGraphStore(self.base_dir, sid,
                                snapshot_every=int(os.getenv("SNAPSHOT_EVERY", "20")))
            st.open()
            self._stores[sid] = st
        return st

    def topics_for(self, club_id: str):
        sid = _safe(club_id)
        ts = self._topics.get(sid)
        if ts is None:
            from content.content_store import TopicStore
            ts = TopicStore(sid)
            self._topics[sid] = ts
        return ts

    def engine_for(self, club_id: str):
        sid = _safe(club_id)
        en = self._engines.get(sid)
        if en is None:
            from content.content_engine import ContentEngine
            en = ContentEngine()
            self._engines[sid] = en
        return en

    def materials(self, club_id: str) -> List[dict]:
        from content.material_analyst import materials_of
        return materials_of(self.store_for(club_id).latest())

    def window(self, club_id: str) -> dict:
        from content.fact_verifier import activity_window
        return activity_window(self.materials(club_id))

    # ---------- 报告行(审核留痕) ----------
    def _append_step(self, club_id: str, step: str, **fields) -> None:
        """把工作流一步写进报告行(design/11 §5.5: 每步可观测)。"""
        import json
        base = os.getenv("REPORT_DIR", os.path.join(_BASE, "data", "reports"))
        os.makedirs(base, exist_ok=True)
        path = os.path.join(base, f"{_safe(club_id)}.jsonl")
        rec = {"ts": now(), "student_id": _safe(club_id), "step": step,
               "phase": phase_of(step), "head_seq": self.store_for(club_id).head_seq}
        rec.update(fields)
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as e:  # noqa: BLE001
            logger.warning("报告行落盘失败: %s", e)

    def report_rows(self, club_id: str, limit: int = 200) -> List[dict]:
        import json
        base = os.getenv("REPORT_DIR", os.path.join(_BASE, "data", "reports"))
        path = os.path.join(base, f"{_safe(club_id)}.jsonl")
        if not os.path.exists(path):
            return []
        rows: List[dict] = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(rec, dict):
                        rows.append(rec)
        except OSError:
            return []
        return rows[-limit:]

    # ---------- 六步: ① 素材录入 ----------
    async def analyze_text(self, club_id: str, text: str) -> dict:
        from content.material_analyst import analyze_material_text
        st = self.store_for(club_id)
        events = st.latest().get("events", {})
        items = await analyze_material_text(text, events,
                                            embedder=getattr(self, "embedder", None))
        self._append_step(club_id, S_INTAKE, action="analyze",
                          sentences=len(items))
        return {"club_id": club_id, "items": items, "head_seq": st.head_seq,
                "step": S_INTAKE, "phase": phase_of(S_INTAKE)}

    def ingest(self, club_id: str, records: List[dict],
               session_id: str = "") -> dict:
        from content.material_analyst import ingest_materials
        st = self.store_for(club_id)
        got = ingest_materials(st, records, session_id=session_id or _safe(club_id))
        self._append_step(club_id, S_INTAKE, action="ingest", count=len(got))
        return {"club_id": club_id, "imported": got, "count": len(got),
                "head_seq": st.head_seq, "step": S_INTAKE,
                "phase": phase_of(S_INTAKE)}

    def seed_demo(self, club_id: str, session_id: str = "demo") -> dict:
        """一键灌入演示语料(历史层 + 当年层, design/11 §10)。"""
        from content import demo_corpus as corpus
        return self.ingest(club_id, corpus.all_materials(), session_id=session_id)

    def state(self, club_id: str) -> dict:
        from content.material_analyst import material_badge
        st = self.store_for(club_id)
        state = st.latest()
        mats = self.materials(club_id)
        return {
            "club_id": club_id,
            "head_seq": st.head_seq,
            "materials": [{**m, "badge": material_badge(m)} for m in mats],
            "count": len(mats),
            "edges": state.get("edges", {}),
            "weak": state.get("weak", []),
            "history": st.history(limit=50),
            "window": self.window(club_id),
            "workflow": {"steps": list(STEP_LABELS.keys()), "phases": WORKFLOW_PHASES},
        }

    # ---------- ② 缺口检测 ----------
    def detect_gaps(self, club_id: str) -> dict:
        from content.fact_verifier import check_list, verify_draft
        mats = self.materials(club_id)
        # 素材库自检: 用空稿走一遍核验(段落级为空, 只剩素材级缺口)。
        # 用户裁决一并传进去: 否则清单里的矛盾项会把用户已经选过的口径又摆回成
        # "按权威度裁定"(design/11 §11: 用户裁决一旦存在就永远优先)。
        res = verify_draft({"topic": "", "segments": []}, mats, self.window(club_id),
                           user_resolutions=self.topics_for(club_id).resolutions())
        self._last_verify[club_id] = res
        self._append_step(club_id, S_GAP, total=res["summary"]["total"],
                          by_type=res["summary"]["by_type"])
        return {"club_id": club_id, "gaps": res["gaps"],
                "check_list": check_list(res["gaps"]),
                "summary": res["summary"], "step": S_GAP, "phase": phase_of(S_GAP)}

    # ---------- ③ 选题成稿 ----------
    def scan_topics(self, club_id: str, persist: bool = True) -> dict:
        from content.topic_gap import scan_topics
        from content.content_versions import versions
        mats = self.materials(club_id)
        st = self.store_for(club_id)
        drafts = versions(club_id)
        topics = scan_topics(mats, drafts, st.latest().get("edges", {}).get("cites", {}),
                             (self._last_verify.get(club_id) or {}).get("gaps") or [],
                             self.window(club_id))
        added = {"added": 0, "total": 0}
        if persist:
            added = self.topics_for(club_id).add_many(topics)
        self._append_step(club_id, S_DRAFT, action="scan_topics",
                          count=len(topics), added=added.get("added"))
        return {"club_id": club_id, "topics": topics, "added": added,
                "step": S_DRAFT, "phase": phase_of(S_DRAFT)}

    def draft(self, club_id: str, topic: Optional[str] = None,
              max_segments: int = 6, use_llm: bool = False,
              intents: Optional[List[str]] = None) -> dict:
        """成稿(决策链 → 段结构)。轨一默认零 LLM; use_llm 时走 P2 闸门。"""
        mats = self.materials(club_id)
        st = self.store_for(club_id)
        topics = self.topics_for(club_id).records()
        if topic:
            topics = [t for t in topics if t.get("subject") == topic] or \
                     [{"subject": topic, "score": 1.0}]
        gap_result = self._last_verify.get(club_id)
        if gap_result is None:
            gap_result = self.detect_gaps(club_id)
        draft = self.engine_for(club_id).draft(
            mats, topics, gap_result, intents=intents,
            window=self.window(club_id), max_segments=max_segments, use_llm=use_llm)

        # 轨一: 落 cites 边(段落 → 素材) + 写报告行(素材图 seq 回滚时一起回退)
        for e in draft.get("cite_edges") or []:
            st.commit("ADD_EDGE", {"graph": "cites", "src": e["src"], "tgt": e["tgt"],
                                   "kind": e.get("kind"), "weight": e.get("weight", 1.0)},
                      session_id=_safe(club_id))
        draft["seq"] = st.head_seq
        draft["ts"] = now()
        self._append_step(club_id, S_DRAFT, action="draft", topic=draft.get("topic"),
                          # 成稿行要带主题与段结构: 复盘节点图(review_graph)由此派生成稿节点,
                          # 采编复盘即以"第几次成稿"为颗粒度(design/11 §3.2)
                          segments=draft.get("segments") or [],
                          seg_count=len(draft.get("segments") or []),
                          converged=draft.get("converged"))
        from content.content_versions import save_version
        save_version(club_id, st.head_seq, draft, track="base",
                     llm_used=bool(use_llm))
        return {"club_id": club_id, "draft": draft, "seq": st.head_seq,
                "step": S_DRAFT, "phase": phase_of(S_DRAFT),
                "degraded": _degradation(draft)}

    # ---------- ④ 事实核验(发布相门禁) ----------
    def verify(self, club_id: str, draft: Optional[dict] = None) -> dict:
        from content.content_engine import ContentEngine
        from content.fact_verifier import check_list, verify_draft
        mats = self.materials(club_id)
        if draft is None:
            draft = self.current_draft(club_id)
        # 带上用户已裁决的矛盾: 让"用户选了哪一版"跨核验轮次有效
        res = verify_draft(draft, mats, self.window(club_id),
                           user_resolutions=self.topics_for(club_id).resolutions())
        ContentEngine.apply_verification(draft, res)
        self._last_verify[club_id] = res
        self._append_step(club_id, S_VERIFY, action="verify",
                          blocking=res["summary"]["blocking"],
                          total=res["summary"]["total"], block=res["block"])
        # 缺口写进素材图 weak(前端左栏与待核实清单共用一份真相)
        st = self.store_for(club_id)
        for g in res["gaps"][:20]:
            st.commit("ADD_WEAK", {"event_id": g.get("source_eid") or "",
                                   "weakness_type": g.get("weakness_type"),
                                   "priority": g.get("priority"),
                                   "reason": g.get("reason"),
                                   "claim": g.get("assertion") or g.get("claim"),
                                   "source_eid": g.get("source_eid") or ""},
                      session_id=_safe(club_id))
        from content.content_versions import save_version
        draft["seq"] = st.head_seq
        # 用户组装的稿存"组装轨"（compose），不覆盖系统成稿的轨一基线：
        # 平台渲染、版本下拉、联动回滚都认轨一，用户的组装稿另有版本可回看。
        is_composed = bool(draft.get("composed"))
        save_version(club_id, st.head_seq, draft,
                     track="compose" if is_composed else "base")
        return {"club_id": club_id, "verify": res, "draft": draft,
                "check_list": check_list(res["gaps"]), "block": res["block"],
                "step": S_VERIFY, "phase": phase_of(S_VERIFY)}

    # ---------- ⑤ 多平台适配(门禁: 有阻断项不许过) ----------
    def platform_versions(self, club_id: str, draft: Optional[dict] = None,
                          force: bool = False) -> dict:
        from content.render import render_all
        mats = self.materials(club_id)
        if draft is None:
            draft = self.current_draft(club_id)
        res = self._last_verify.get(club_id)
        if res is None:
            res = self.verify(club_id, draft)["verify"]
        if res.get("block") and not force:
            self._append_step(club_id, S_PLATFORM, action="blocked",
                              blocking=res["summary"]["blocking"])
            return {"club_id": club_id, "blocked": True,
                    "reason": "存在阻断项(矛盾/无据/涉隐私), 不许进入多平台适配",
                    "gaps": [g for g in res["gaps"] if g.get("blocking")],
                    "step": S_PLATFORM, "phase": phase_of(S_PLATFORM)}
        versions = render_all(draft.get("segments") or [], None, mats)
        from content.content_versions import get_version, save_version
        seq = self.store_for(club_id).head_seq
        # 平台渲染结果按 **平台轨道** 存(design/11 §5.4: 两轨存版本), 但段结构仍写稿件
        # 的六段 —— 否则多平台适配会把稿件版本基线改写成某个平台的段名, 下一次渲染
        # 就再也取不到 lead/highlight 等段(确定性渲染变成一串空段)。
        # 该 seq 还没有稿件基线时(例如先核验后适配), 至少留一版基线, 版本下拉不空。
        have_base = get_version(club_id, seq, "base") is not None
        for p, v in versions.items():
            if not have_base:
                # 这一版还没有轨一基线（例如用户在组装稿上直接核验并适配）：
                # **不要**拿平台渲染结果去当基线 —— 那会把"当前稿件"写成某个平台的段名。
                # 平台版本本身已经随响应返回，前端右栏照常显示。
                continue
            save_version(club_id, seq, {**draft, "segments": draft.get("segments") or []},
                         track=f"base:{p}", text=v.get("text"))
        self._append_step(club_id, S_PLATFORM, action="adapt",
                          platforms=list(versions.keys()),
                          over=[p for p, v in versions.items() if v["limit"]["over"]])
        return {"club_id": club_id, "blocked": False, "platforms": versions,
                "step": S_PLATFORM, "phase": phase_of(S_PLATFORM)}

    # ---------- ⑥ 审核留痕 ----------
    def review_record(self, club_id: str, draft: Optional[dict] = None) -> dict:
        """导出《审核记录》(design/11 §13 C5: 对外叫「审核记录」)。

        含: 素材清单(带权威度) / 缺口与处置 / 稿件版本 / LLM 用量 / 回滚次数。
        它是给审稿人用的**证据与记录工具**, 不是审核结论(design/11 §2 边界)。
        """
        from components import llm_gate
        from content.content_versions import versions
        from content.fact_verifier import check_list, blocking_gaps
        st = self.store_for(club_id)
        mats = self.materials(club_id)
        res = self._last_verify.get(club_id) or {}
        gaps = res.get("gaps") or []
        rollbacks = 0
        rl = getattr(st, "_rollback_log", None)
        if rl and os.path.exists(rl):
            try:
                with open(rl, "r", encoding="utf-8") as f:
                    rollbacks = sum(1 for line in f if line.strip())
            except OSError:
                pass
        if draft is None:
            draft = self.current_draft(club_id)
        rec = {
            "club_id": club_id,
            "generated_at": now(),
            "head_seq": st.head_seq,
            "materials": [{"eid": m.get("eid"), "subject": m.get("subject"),
                           "type": m.get("type"), "source": m.get("source"),
                           "authority": m.get("authority"),
                           "verified": bool(m.get("verified")),
                           "sensitive": bool(m.get("sensitive")),
                           "content": (m.get("content") or "")[:200]} for m in mats],
            "gaps": check_list(gaps),
            "blocking": blocking_gaps(gaps),
            "draft": {"topic": (draft or {}).get("topic"),
                      "segments": (draft or {}).get("segments") or [],
                      "block": bool((draft or {}).get("block"))},
            "versions": versions(club_id, limit=20),
            "llm": llm_gate.counters(),
            "rollbacks": rollbacks,
            "steps": self.report_rows(club_id),
            # 报告行按内容域命名再暴露一次(前端与导出都按 report_rows 取)
            "report_rows": self.report_rows(club_id),
            "resolutions": self.topics_for(club_id).resolutions(),
            "disclaimer": ("本记录由系统确定性生成, 提供核验证据与操作留痕; "
                           "审核结论由人作出。"),
        }
        self._append_step(club_id, S_REVIEW, action="review_record",
                          materials=len(rec["materials"]), gaps=len(rec["gaps"]))
        return {"club_id": club_id, "record": rec, "step": S_REVIEW,
                "phase": phase_of(S_REVIEW)}

    # ---------- 稿件当前态与回滚联动 ----------
    def re_draft_with_resolution(self, club_id: str, chosen_eid: str,
                                 seg_id: str = "") -> dict:
        """把用户裁决(选用哪条素材)写进缺口状态, 然后按同一决策链重出一稿并重新核验。

        零 LLM: 决策链本来就是确定性的, 用户选了哪一版就照哪一版写。
        这一步是"人在环里"的闭环 —— 演示里点一次「选它」, 稿件当场变。
        """
        res = self._last_verify.get(club_id) or self.detect_gaps(club_id)
        ts = self.topics_for(club_id)
        gaps = []
        marked_key = ""
        for g in res.get("gaps") or []:
            g = dict(g)
            cand_eids = [c.get("eid") for c in (g.get("candidates") or [])]
            if g.get("weakness_type") == "conflict" and chosen_eid in cand_eids:
                g["resolved"] = True
                g["resolution"] = "user"
                g["chosen_eid"] = chosen_eid
                g["resolution_note"] = f"用户选定 {chosen_eid} 的口径"
                marked_key = g.get("claim_key") or ""
                ts.add_resolution(marked_key, chosen_eid, seg_id=seg_id,
                                  note="用户点选官方口径")
            gaps.append(g)
        marked = {"gaps": gaps, "summary": res.get("summary") or {}}
        self._last_verify[club_id] = marked

        mats = self.materials(club_id)
        st = self.store_for(club_id)
        topics = self.topics_for(club_id).records()
        draft = self.engine_for(club_id).draft(
            mats, topics, marked, window=self.window(club_id))
        for e in draft.get("cite_edges") or []:
            st.commit("ADD_EDGE", {"graph": "cites", "src": e["src"], "tgt": e["tgt"],
                                   "kind": e.get("kind"), "weight": e.get("weight", 1.0)},
                      session_id=_safe(club_id))
        draft["seq"] = st.head_seq
        draft["ts"] = now()
        draft["user_resolution"] = {"chosen_eid": chosen_eid, "seg_id": seg_id}
        self._append_step(club_id, S_DRAFT, action="redraft_after_resolution",
                          topic=draft.get("topic"), segments=draft.get("segments") or [],
                          seg_count=len(draft.get("segments") or []),
                          chosen_eid=chosen_eid)
        from content.content_versions import save_version
        save_version(club_id, st.head_seq, draft, track="base")
        # 注意: 裁决记录已经落盘(TopicStore.add_resolution), 所以下一次核验仍会尊重它;
        # 这里只清掉"内存里那次已消费的核验结果", 让核验按新稿重算。
        self._last_verify.pop(club_id, None)
        v = self.verify(club_id, draft)
        return {"seq": st.head_seq,
                "resolution_key": marked_key,
                "segments": [{"seg_id": s.get("seg_id"), "kind": s.get("kind"),
                              "text": s.get("text"), "status": s.get("status")}
                             for s in draft.get("segments") or []],
                "block": v.get("block"),
                "gaps_by_type": (v.get("verify") or {}).get("summary", {}).get("by_type")}

    def current_draft(self, club_id: str) -> dict:
        """当前稿件 = 轨一事实基线（系统成稿）。

        平台渲染、多平台适配、联动回滚都认这一版（design/11 §5.4/§9 A2）。
        用户拖拽组装的那一版**不在这里**：它在 compose 轨，由 `compose_draft()` 取，
        并随 `/api/content/versions` 一起给前端恢复用（刷新后看到自己改的稿）。
        """
        from content.content_versions import baseline, latest
        rec = baseline(club_id) or latest(club_id, prefer_compose=False)
        if rec is None:
            return {"topic": "", "segments": [], "seq": self.store_for(club_id).head_seq}
        return {"topic": rec.get("topic"), "segments": rec.get("segments") or [],
                "seq": rec.get("seq"), "text": rec.get("text"),
                "block": rec.get("block"), "track": rec.get("track")}

    def compose_draft(self, club_id: str) -> Optional[dict]:
        """用户拖拽组装的那一版（compose 轨）；没有就返回 None。"""
        from content.content_versions import _read, versions_path
        rows = [r for r in _read(versions_path(club_id)) if r.get("track") == "compose"]
        if not rows:
            return None
        rows.sort(key=lambda r: -int(r.get("seq") or 0))
        r0 = rows[0]
        return {"topic": r0.get("topic"), "segments": r0.get("segments") or [],
                "seq": r0.get("seq"), "text": r0.get("text"),
                "block": r0.get("block"), "track": "compose"}

    def draft_at(self, club_id: str, seq: int) -> dict:
        """按 seq 取稿件版本(与素材图时间点对齐)。"""
        from content.content_versions import get_any_version
        rec = get_any_version(club_id, seq)
        if rec is None:
            return {"error": f"seq={seq} 没有稿件版本"}
        return {"topic": rec.get("topic"), "segments": rec.get("segments") or [],
                "seq": seq, "text": rec.get("text"), "block": rec.get("block"),
                "track": rec.get("track")}

    def rollback(self, club_id: str, to_seq: int, reason: str = "") -> dict:
        """回滚素材图到某 seq → 稿件与平台版本**联动回退**(design/11 §9 验收 A2)。

        素材图回滚会截断 cites 边与 weak; 稿件版本按 seq 取回当版, 因此"回到审前状态"
        是一步操作(design/11 §11 协作分: 可回滚)。
        """
        st = self.store_for(club_id)
        try:
            r = st.rollback(int(to_seq), session_id=_safe(club_id),
                            reason=reason or "content_rollback")
        except ValueError as e:
            return {"error": str(e)}
        draft = self.draft_at(club_id, int(to_seq)) if int(to_seq) > 0 else {}
        self._last_verify.pop(club_id, None)
        return {"club_id": club_id, "rolled_back": r.get("rolled_back"),
                "seq": int(to_seq), "draft": draft,
                "cites": len(st.latest().get("edges", {}).get("cites", {})),
                "head_seq": st.head_seq}

    def snapshot(self, club_id: str, reason: str = "manual") -> int:
        return self.store_for(club_id).snapshot(reason=reason)

    # ---------- 仪表盘(协作/宣发分的量化支撑) ----------
    def dashboard(self, club_id: str) -> dict:
        from components import llm_gate
        st = self.store_for(club_id)
        mats = self.materials(club_id)
        state = st.latest()
        gaps = (self._last_verify.get(club_id) or {}).get("gaps") or []
        by_type: Dict[str, int] = {}
        for g in gaps:
            by_type[g.get("weakness_type")] = by_type.get(g.get("weakness_type"), 0) + 1
        counters = llm_gate.counters()
        cert = len(mats) + len(gaps) + len(state.get("edges", {}).get("cites", {}))
        by_point = counters.get("by_point", {}) or {}
        # 成功的调用与"被闸门拦下的尝试"要分开说: 审计行两种都记(design/11 §6),
        # 但人机分工要讲的是**真正调了 LLM 的那几次**(被拦下的尝试不消耗 LLM)。
        successful = sum(int(v.get("ok") or 0) for v in by_point.values())
        denied = sum(int(v.get("error") or 0) for v in by_point.values())
        return {
            "club_id": club_id,
            "materials": len(mats),
            "authority_mean": _mean(m.get("authority") for m in mats),
            "gaps": {"total": len(gaps), "by_type": by_type,
                     "blocking": sum(1 for g in gaps if g.get("blocking"))},
            "cites": len(state.get("edges", {}).get("cites", {})),
            "topics": self.topics_for(club_id).count(),
            "llm": {"default": counters.get("default", 0),
                    "authorized": counters.get("authorized", 0),
                    "by_point": by_point,
                    "successful": successful,
                    "denied": denied,
                    "scope": "llm_gate.jsonl 当前文件累计(清空该文件即从 0 起算)"},
            "deterministic_ops": cert,
            "human_division": (f"{cert} 次确定性操作 / {successful} 次 LLM 调用"
                               + (f"（另有 {denied} 次尝试被闸门拦下，未消耗 LLM）"
                                  if denied else "")),
            "rollbacks": _count_lines(getattr(st, "_rollback_log", "")),
            "head_seq": st.head_seq,
        }


def _degradation(draft: dict) -> dict:
    """降级信息: 这一稿有多少段是靠模板/缺口兜底出的(design/11 §12 R1 的量化)。"""
    steps = draft.get("steps") or []
    by_source: Dict[str, int] = {}
    for s in steps:
        if not isinstance(s, dict):
            continue
        name = s.get("source_used") or s.get("source") or "?"
        by_source[name] = by_source.get(name, 0) + 1
    return {"by_source": by_source,
            "llm_segments": by_source.get("llm_segment", 0),
            "template_segments": by_source.get("template", 0)
            + by_source.get("template_empty", 0),
            "gap_segments": by_source.get("gap_segment", 0)
            + by_source.get("gap_template", 0)}


def _mean(vals) -> float:
    xs = [float(v) for v in vals if v is not None]
    return round(sum(xs) / len(xs), 4) if xs else 0.0


def _count_lines(path: str) -> int:
    if not path or not os.path.exists(path):
        return 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 0


def _safe(sid: str) -> str:
    return "".join(c for c in (sid or "") if c.isalnum() or c in "-_") or "default"


_RUNTIME: Optional[ContentRuntime] = None


def get_content_runtime() -> ContentRuntime:
    global _RUNTIME
    if _RUNTIME is None:
        _RUNTIME = ContentRuntime()
    return _RUNTIME


__all__ = ["ContentRuntime", "get_content_runtime", "STEP_LABELS", "WORKFLOW_PHASES",
           "S_INTAKE", "S_GAP", "S_DRAFT", "S_VERIFY", "S_PLATFORM", "S_REVIEW"]
