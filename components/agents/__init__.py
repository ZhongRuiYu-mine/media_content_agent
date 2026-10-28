# coding: utf-8
"""components/agents — HOTS swarmflow 脚本(采编复盘的深度命令)。

- hots.py: 「重核验 / 重新成段」两条命令 —— 两个 worker 并行跑确定性核验与多平台改写,
  产出仍要过断言级 diff(数字/人名/职务/时间/地点集合必须与事实基线一致)。
  领域工具在 components/content_tools.py(素材库 / 待核实清单 / 当前稿件 / 选题库)。
"""
