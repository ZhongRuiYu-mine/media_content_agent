# coding: utf-8
"""tools/prepare_demo.py — 演示语料一键准备与自检(design/11 §10 + §9 T4.1)。

做的事(全部零 LLM, 断网可跑):
1. 清掉该社团的演示数据(素材图 / 稿件版本 / 选题库 / 报告行), 保证每次演示从同一个起点开始;
2. 灌入演示语料(历史层 + 当年层);
3. 跑一遍完整链路: 缺口检测 → 选题扫描 → 成稿 → 核验 → 多平台适配;
4. 导出《审核记录》与「宣发包」到 `data/demo_out/`;
5. 打印预期结果自检表(缺口分布 / 溯源边 / 平台字数 / 降级构成), 与 design/11 §10 末行的
   预期分布对照, 一眼看出语料或链路是否走偏。

用法:
    D:\\jiuwen\\Scripts\\python.exe tools\\prepare_demo.py
    D:\\jiuwen\\Scripts\\python.exe tools\\prepare_demo.py --club demo_club --keep
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJ = os.path.dirname(_HERE)
sys.path.insert(0, _PROJ)
os.chdir(_PROJ)

os.environ.setdefault("EMBEDDING_OFF", "1")          # 纯规则路径(演示不依赖向量模型)
os.environ.setdefault("LLM_GATE_MODE", "auto_deny")  # 默认零 LLM: 证明确定性链路自己就能跑通
os.environ.setdefault("TOPIC_STORE_BACKEND", "file")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

# design/11 §10 末行的预期分布(演示自检的对照表)
EXPECTED_MIN = {"no_source": 1, "conflict": 1, "isolated": 1, "stale": 1, "sensitive": 1}


def _wipe(club: str) -> None:
    """清掉该社团的演示数据(只删这几条路径, 不碰别的社团)。

    顺带清空 LLM 审计文件与**用户裁决记录**: 仪表盘讲的是"这次演示花了多少次 LLM",
    上一次运行留下的审计行会让这个数字失真; 裁决记录是跨核验轮次有效的(设计要求),
    不清掉的话第二次演示时矛盾项一开始就带"已由用户裁定".
    """
    import json
    base = os.getenv("STORAGE_BASE_DIR", os.path.join(_PROJ, "data", "graphs"))
    shutil.rmtree(os.path.join(base, club), ignore_errors=True)
    vdir = os.getenv("CONTENT_VERSIONS_DIR", os.path.join(_PROJ, "data", "content_versions"))
    for p in (os.path.join(vdir, f"{club}.jsonl"),):
        if os.path.exists(p):
            os.remove(p)
    tdir = os.getenv("TOPIC_BANK_DIR", os.path.join(_PROJ, "data", "topic_bank"))
    p = os.path.join(tdir, f"{club}.json")
    if os.path.exists(p):
        os.remove(p)
    rdir = os.getenv("REPORT_DIR", os.path.join(_PROJ, "data", "reports"))
    p = os.path.join(rdir, f"{club}.jsonl")
    if os.path.exists(p):
        os.remove(p)
    sdir = os.getenv("RESOLUTION_DIR", os.path.join(_PROJ, "data", "resolutions"))
    p = os.path.join(sdir, f"{club}.json")
    if os.path.exists(p):
        os.remove(p)
    gate = os.getenv("LLM_GATE_LOG",
                     os.path.join(_PROJ, "data", "logs", "llm_gate.jsonl"))
    if os.path.exists(gate):
        os.remove(gate)
    _ = json  # 保留导入以示读的是 JSON 落盘格式


def _write_out(name: str, text: str) -> str:
    out = os.path.join(_PROJ, "data", "demo_out")
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--club", default="demo_club", help="社团/部门 id(默认 demo_club)")
    ap.add_argument("--keep", action="store_true", help="保留已有数据(不清库)")
    args = ap.parse_args()
    club = args.club

    from content.content_runtime import ContentRuntime
    from content.render import export_package

    if not args.keep:
        _wipe(club)
    rt = ContentRuntime()
    st = rt.store_for(club)

    print("=" * 68)
    print("演示准备: 新媒体信宣智能体 · 社团活动稿件核验与留痕")
    print("=" * 68)

    seeded = rt.seed_demo(club)
    print(f"[1] 素材入库: {seeded['count']} 条(历史层 + 当年层), 素材图 seq={seeded['head_seq']}")
    mats = rt.materials(club)
    bands = {}
    for m in mats:
        from graphstore.types import authority_band
        b = authority_band(m.get("authority") or 0.0)
        bands[b] = bands.get(b, 0) + 1
    print("    权威度分布: " + "、".join(f"{k}={v}" for k, v in sorted(bands.items())))

    gaps = rt.detect_gaps(club)
    by = dict(gaps["summary"]["by_type"])
    print(f"[2] 素材库自检缺口: 共 {gaps['summary']['total']} 条  " +
          "、".join(f"{k}={v}" for k, v in by.items()))

    # 无据(no_source)只在**稿件**上出现(素材库自检没有句子可比), 所以拿演示示例稿再跑一遍,
    # 这样自检表覆盖完整五类(design/11 §10 末行的预期分布是"缺口清单"的分布)。
    from content import demo_corpus as corpus
    from content.fact_verifier import verify_draft
    sample = verify_draft({"topic": "本届编程马拉松比赛",
                           "segments": [dict(s) for s in corpus.DEMO_DRAFT_SEGMENTS]},
                          rt.materials(club), rt.window(club))
    by_draft = sample["summary"]["by_type"]
    print("    示例稿核验: " + "、".join(f"{k}={v}" for k, v in by_draft.items())
          + f"  阻断={'是' if sample['block'] else '否'}")
    combined = {k: max(by.get(k, 0), by_draft.get(k, 0)) for k in EXPECTED_MIN}
    miss = [k for k, v in EXPECTED_MIN.items() if combined.get(k, 0) < v]
    print("    合计(素材库 + 示例稿): " + "、".join(f"{k}={v}" for k, v in combined.items()))
    print("    " + ("五类覆盖齐全" if not miss else f"⚠ 缺少: {miss}"))

    topics = rt.scan_topics(club)
    print(f"[3] 选题扫描: {len(topics['topics'])} 条")
    for t in topics["topics"][:5]:
        print(f"    - [{t['kind']}] {t['subject']}  分数={t['score']}  {t['reason']}")

    d = rt.draft(club)
    dr = d["draft"]
    print(f"[4] 成稿: {len(dr['segments'])} 段, 主题「{dr['topic']}」, "
          f"收敛={dr['converged']}, 来源构成={d['degraded']['by_source']}")

    v = rt.verify(club)
    print(f"[5] 事实核验: 阻断={'是' if v['block'] else '否'}, "
          f"缺口 {v['verify']['summary']['total']} 条"
          + (f", 阻断项 {v['verify']['summary']['blocking']} 条" if v["block"] else ""))
    print(f"    溯源链 cites 边: {len(st.latest()['edges']['cites'])} 条"
          f"(每段都指得回素材)")

    pv = rt.platform_versions(club, force=True)
    plats = pv.get("platforms") or {}
    print("[6] 多平台适配: " + "、".join(
        f"{p}={x['limit']['count']}/{x['limit']['limit']}字"
        + ("(超限已按句边界收敛)" if x.get("trimmed") else "")
        for p, x in plats.items()))

    rec = rt.review_record(club)
    from server import _review_record_markdown
    p1 = _write_out(f"审核记录_{club}.md", _review_record_markdown(rec["record"]))
    p2 = _write_out(f"宣发包_{club}.md",
                    export_package("", plats, dr.get("topic") or ""))
    # 示例稿与它的待核实清单: 演示时用来展示"被门禁拦下的那一稿"长什么样
    from content.fact_verifier import check_list
    lines = ["# 示例稿核验 · 被门禁拦下的那一稿", "",
             f"> 主题: 本届编程马拉松比赛 · 阻断: {'是' if sample['block'] else '否'} · "
             f"缺口合计 {sample['summary']['total']} 条", "",
             "## 稿件(逐段, 带来源指针)", ""]
    for s in corpus.DEMO_DRAFT_SEGMENTS:
        lines.append(f"- **{s.get('kind')}** {s.get('text')}")
    lines += ["", "## 待核实清单", "",
              "| 类型 | 断言 | 素材 | 阻断 | 原因 |", "|---|---|---|---|---|"]
    for c in check_list(sample["gaps"]):
        lines.append(f"| {c['label']} | {c['assertion']} | {c['source_eid'] or '—'} | "
                     f"{'是' if c['blocking'] else '否'} | {c['reason']} |")
    lines += ["", "## 多平台适配的门禁判定", "",
              f"该稿阻断 = `{sample['block']}`; 有阻断项时 `/api/content/platforms` 默认拒绝适配,",
              "需人工明确 `force=true` 才放行(放行动作会记进审核记录)。"]
    p3 = _write_out(f"示例稿核验_{club}.md", "\n".join(lines))
    dash = rt.dashboard(club)
    print(f"[7] 导出: {os.path.relpath(p1, _PROJ)} / {os.path.relpath(p2, _PROJ)}"
          f" / {os.path.relpath(p3, _PROJ)}")
    print(f"[8] 仪表盘: {dash['human_division']}; 回滚次数={dash['rollbacks']}")
    print("    本稿缺口: " + "、".join(
        f"{k}={v}" for k, v in (v.get("verify") or {}).get("summary", {})
        .get("by_type", {}).items())
        + f"; 多平台 blocked={'是' if pv.get('blocked') else '否'}")

    print("-" * 68)
    print("演示起点已就绪。启动服务后打开 http://127.0.0.1:8080/workbench")
    print("数据库路径: STORAGE_BASE_DIR=" + os.getenv("STORAGE_BASE_DIR", "data/graphs"))
    print("彩排脚本见 演示脚本.md")
    bad = bool(miss)
    print("自检: " + ("⚠ 有缺口类型没覆盖, 检查语料" if bad else "通过(五类缺口齐全, 零 LLM)"))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
