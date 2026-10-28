# coding: utf-8
"""content/workflow.py — 六步两相常量(design/11 §5.5)。

命题流程: 素材录入 → 缺口检测 → 选题成稿 → 事实核验 → 多平台适配 → 审核留痕。
本项目的组织: 六步分两相 ——

  采编相(editorial): 素材录入 / 缺口检测 / 选题成稿, 由 content 引擎状态机推进;
  发布相(publish):   事实核验 / 多平台适配 / 审核留痕, 成稿后由发布链路与采编复盘团队执行。

phase 写进稿件状态与报告行(step 字段), 运行时可观测, 验收/答辩可指认每一步。

注意: 事实核验(fact_verify)在实现上是"发布相的门禁"——它拦住的稿件不许进入多平台适配。
"""
from __future__ import annotations

WORKFLOW_STEPS = [
    "material_intake",   # 素材录入(切句 + 逐句裁决 → 素材图 + 来源档位)
    "gap_detect",        # 缺口检测(无据/矛盾/孤证/过期/涉隐私, 零 LLM)
    "topic_draft",       # 选题成稿(决策链 → 段结构)
    "fact_verify",       # 事实核验(断言溯源, 阻断无据)
    "platform_adapt",    # 多平台适配(同一稿 → N 个平台版本)
    "review_log",        # 审核留痕(证据与记录导出)
]

WORKFLOW_PHASES = {
    "editorial": WORKFLOW_STEPS[:3],   # 采编相
    "publish": WORKFLOW_STEPS[3:],     # 发布相
}

STEP_LABELS = {
    "material_intake": "素材录入", "gap_detect": "缺口检测",
    "topic_draft": "选题成稿", "fact_verify": "事实核验",
    "platform_adapt": "多平台适配", "review_log": "审核留痕",
}


def phase_of(step: str) -> str:
    for ph, steps in WORKFLOW_PHASES.items():
        if step in steps:
            return ph
    return "editorial"


__all__ = ["WORKFLOW_STEPS", "WORKFLOW_PHASES", "STEP_LABELS", "phase_of"]
