# coding: utf-8
"""content — 内容域(新媒体信宣智能体)核心包(design/11)。

模块分工(零 LLM 优先, 确定性优先):

| 模块 | 职责 | design/11 |
|---|---|---|
| `material_ops.py`     | 权威度推断 / 断言抽取 / cites 溯源边推导 | §5.1 |
| `material_analyst.py` | 素材录入(切句 → 逐句裁决 → 素材图) | §5.5 ① |
| `fact_verifier.py`    | 事实核验: 断言核验 + 五类缺口 + 门禁 | §4.3 / §5.1 |
| `topic_gap.py`        | 缺口驱动选题(三维度确定性扫描 + 排序) | §5.2 |
| `content_engine.py`   | 成稿引擎: 决策链 + 段状态机 + 收敛 | §5.3 |
| `content_store.py`    | 选题库(Mongo 优先, File 兜底 + 轮询) | §5.2 / §12 R5 |
| `content_versions.py` | 稿件版本(版本号 = 图 seq, 两轨都存) | §5.4 |
| `render.py`           | 双轨渲染 + 断言级 diff(验收 A5) | §5.4 |
| `platform_spec.py`    | 平台规范(字数上限/段结构/分镜) | §5.4 |
| `content_runtime.py`  | 编排层: 六步两相 + 门禁 + 审核留痕导出 | §5.5 |
| `demo_corpus.py`      | 演示语料(历史层 + 当年层) | §10 |

导入约定: **不做包级 re-export**。各模块按需 `from content.xxx import ...`,
这样 `material_ops`(纯规则)不会因为别的模块 import 重型依赖而被牵连。
"""
from __future__ import annotations

__all__ = []
