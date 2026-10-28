# coding: utf-8
"""tools/check_acceptance.py — design/11 §9 四条验收一键核验。

把方案里写明的验收标准逐条跑一遍, 每条给出「通过/不通过 + 证据数字」:

    A1  零 LLM 模式下跑通"散料 → 素材图 + 缺口清单", 缺口五类各有至少 1 例
    A2  素材图 → 稿件 + 2 平台版本 + 阻断项; 回滚素材图到某 seq, 稿件与平台版本同步回退
    A3  起一个采编复盘会话, 6 角色中至少 3 个有实质输出(离线只验证名册与快照绑定)
    A5  轨二润色前后, 数字集合与专名集合完全一致(脚本比对, 不靠肉眼)

用法:
    D:\\jiuwen\\Scripts\\python.exe tools\\check_acceptance.py
"""
from __future__ import annotations

import os
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJ = os.path.dirname(_HERE)
sys.path.insert(0, _PROJ)
os.chdir(_PROJ)

# 隔离目录放在项目 data/ 下(gitignore 已忽略; 系统临时目录在受限沙箱里可能不可写)
_TMP = os.path.join(_PROJ, "data", "_acceptance_tmp")
shutil.rmtree(_TMP, ignore_errors=True)
os.makedirs(_TMP, exist_ok=True)
os.environ["STORAGE_BASE_DIR"] = os.path.join(_TMP, "graphs")
os.environ["REPORT_DIR"] = os.path.join(_TMP, "reports")
os.environ["CONTENT_VERSIONS_DIR"] = os.path.join(_TMP, "versions")
os.environ["TOPIC_BANK_DIR"] = os.path.join(_TMP, "topics")
os.environ["REVIEWS_HOME"] = os.path.join(_TMP, "reviews")
os.environ["LLM_GATE_LOG"] = os.path.join(_TMP, "llm_gate.jsonl")
os.environ["EMBEDDING_OFF"] = "1"
os.environ["LLM_GATE_MODE"] = "auto_deny"
os.environ["TOPIC_STORE_BACKEND"] = "file"
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

CLUB = "acceptance_club"
RESULTS = []


def record(code: str, title: str, ok: bool, evidence: str) -> None:
    RESULTS.append((code, title, ok, evidence))
    print(f"  {'通过' if ok else '不通过'}  {code} {title}")
    print(f"        证据: {evidence}")


def main() -> int:
    from components import llm_gate
    from content import demo_corpus as corpus
    from content.content_runtime import ContentRuntime
    from content.fact_verifier import GAP_TYPES, verify_draft
    from content.render import assertion_diff

    rt = ContentRuntime(base_dir=os.environ["STORAGE_BASE_DIR"])
    print("=" * 70)
    print("design/11 §9 验收核验 · 新媒体信宣智能体")
    print("=" * 70)

    # ---------- 准备: 散料走一遍完整链路(零 LLM) ----------
    print("\n【准备】散料 → 素材图 → 缺口 → 成稿 → 核验")
    items = None
    import asyncio
    items = asyncio.run(rt.analyze_text(CLUB, corpus.SCATTERED_TEXT))
    seeded = rt.seed_demo(CLUB)
    mats = rt.materials(CLUB)
    print(f"  散料逐句裁决 {len(items)} 句; 素材入库 {seeded['count']} 条")

    # ---------- A1 ----------
    sample = verify_draft({"topic": "本届编程马拉松比赛",
                           "segments": [dict(s) for s in corpus.DEMO_DRAFT_SEGMENTS]},
                          mats, rt.window(CLUB))
    by = sample["summary"]["by_type"]
    material_gaps = rt.detect_gaps(CLUB)["summary"]["by_type"]
    combined = {t: max(by.get(t, 0), material_gaps.get(t, 0)) for t in GAP_TYPES}
    a1_ok = (all(combined.get(t, 0) >= 1 for t in GAP_TYPES)
             and len(items) >= 5
             and llm_gate.gate_mode() == "auto_deny"
             and not llm_gate.allowed("P2") and not llm_gate.allowed("P8"))
    record("A1", "零 LLM 跑通散料→素材图+缺口清单, 五类齐全", a1_ok,
           f"逐句裁决 {len(items)} 句; 缺口分布 {combined}; "
           f"闸门={llm_gate.gate_mode()}(默认点也不放行)")

    # ---------- A2 ----------
    rt.scan_topics(CLUB)
    rt.draft(CLUB)
    v = rt.verify(CLUB)
    pv = rt.platform_versions(CLUB, force=True)
    plats = list((pv.get("platforms") or {}).keys())
    cites_before = len(rt.store_for(CLUB).latest()["edges"]["cites"])
    head_before = rt.store_for(CLUB).head_seq
    rb = rt.rollback(CLUB, 3, reason="acceptance A2")
    cites_after = len(rt.store_for(CLUB).latest()["edges"]["cites"])
    mats_after = len(rt.materials(CLUB))
    a2_ok = (len(plats) >= 2 and cites_before >= 1 and rb.get("rolled_back") is True
             and cites_after == 0 and mats_after == 3)
    record("A2", "素材图 → 稿件 + ≥2 平台版本; 回滚后稿件与平台版本同步回退", a2_ok,
           f"平台 {plats}; cites 边 {cites_before}→{cites_after}; "
           f"素材 {seeded['count']}→{mats_after}; seq {head_before}→{rb.get('seq')}")

    # ---------- A3 ----------
    from components import review_graph, review_runtime
    rt.draft(CLUB)                                   # 回滚后重新成稿, 准备复盘节点
    graph = review_graph.build_graph(CLUB)
    node = [n for n in graph["nodes"] if n["kind"] == "draft"]
    experts = review_runtime.ReviewRuntime.experts()
    snap = None
    if node:
        from components import review_bridge
        snap = review_bridge.snapshot(CLUB, node[-1]["node_id"])
    a3_ok = (len(experts) == 6 and bool(node) and bool(snap)
             and "error" not in (snap or {})
             and len([e for e in experts if e.get("desc")]) >= 3
             and "materials" in (snap or {}) and "check_list" in (snap or {}))
    record("A3", "采编复盘: 6 角色名册齐全 + 绑定当时素材库/稿件快照", a3_ok,
           f"角色 {[e['id'] for e in experts]}; 有职责定义的角色 "
           f"{len([e for e in experts if e.get('desc')])}/6; "
           f"成稿节点 {len(node)} 个; 快照素材 {len((snap or {}).get('materials') or [])} 条, "
           f"待核实 {len((snap or {}).get('check_list') or [])} 条; "
           f"(真实 LLM 会话需 Key, 见 test/_smoke_team.py)")

    # ---------- A5 ----------
    base = ("11月20日, 本届编程马拉松比赛在第三报告厅举行, 实际到场183人, "
            "参赛队伍12支, 指导老师王老师做赛前动员。")
    cases = {
        "只改语序/连接词": ("11月20日, 本届编程马拉松比赛在第三报告厅如期举行, 到场人数为183人, "
                            "共有12支参赛队伍, 指导老师王老师到场做赛前动员。", True),
        "改数字 183→300": (base.replace("183", "300"), False),
        "改场地": (base.replace("第三报告厅", "大学生活动中心"), False),
        "改时间": (base.replace("11月20日", "11月21日"), False),
        "新增无据表述": (base + " 本次比赛反响热烈。", False),
        "删掉人名": (base.replace("指导老师王老师做赛前动员", ""), False),
    }
    rows = []
    a5_ok = True
    for name, (text, want_ok) in cases.items():
        diff = assertion_diff(base, text)
        hit = diff["ok"] is want_ok
        a5_ok = a5_ok and hit
        rows.append(f"{name}→{'一致' if diff['ok'] else '拦下'}{'✓' if hit else '✗'}")
    record("A5", "轨二润色与轨一断言级 diff(数字/专名集合必须完全一致)", a5_ok,
           "; ".join(rows))

    # ---------- 汇总 ----------
    print("\n" + "=" * 70)
    bad = [r for r in RESULTS if not r[2]]
    for code, title, ok, _ev in RESULTS:
        print(f"  [{'PASS' if ok else 'FAIL'}] {code} {title}")
    print("=" * 70)
    print(f"合计 {len(RESULTS)} 条验收: {len(RESULTS) - len(bad)} 通过 / {len(bad)} 不通过")
    return 1 if bad else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
