# coding: utf-8
"""components/llm_gate.py — LLM 调用审计与统计(T2.6, design/06 §5)。

- 调用点常量表(design/06 §1): P2/P3 为默认点, P6 为授权点;
- audit(): 追加一条 jsonl 审计记录(不含回答全文, 隐私);
- counters(): 读取审计文件, 汇总 default/authorized 两类计数与分点统计,
  直接支撑"最大程度降低 LLM"铁律的量化验证。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Dict

# 调用点登记表(design/06 治理 + design/11 §6 内容域重定义)。
# 未登记的点一律不放行; default_allowed=False 为授权点(必须用户显式点按钮)。
LLM_CALL_POINTS = {
    # ---- 默认点: 流程内不打断用户 ----
    "P2": {"label": "批量生成段提纲(缺口驱动)", "default_allowed": True, "cost_label": "1"},
    "P8": {"label": "选题(RAG 素材库)", "default_allowed": True, "cost_label": "1"},
    "P21": {"label": "跨平台改写(一稿多版)", "default_allowed": True, "cost_label": "1"},
    "P22": {"label": "事实核验兜底(两源冲突判定)", "default_allowed": True, "cost_label": "1"},
    "P14": {"label": "素材批量导入解析", "default_allowed": True, "cost_label": "1"},
    # ---- 授权点: 必须用户在场并点授权 ----
    "P24": {"label": "润色/改语气/控字数", "default_allowed": False, "cost_label": "1"},
    "P25": {"label": "模仿往期稿件风格", "default_allowed": False, "cost_label": "1"},
    "P26": {"label": "合规与风险复核", "default_allowed": False, "cost_label": "1"},
    "P28": {"label": "配图文案/短视频分镜", "default_allowed": False, "cost_label": "1"},
    # ---- 预留点(已登记、尚未接线; 有调用点才允许) ----
    "P3": {"label": "质量评分", "default_allowed": True, "cost_label": "1"},
    "P5": {"label": "文体意图兜底打标", "default_allowed": False, "cost_label": "1"},
    "P9": {"label": "素材关系抽取(temporal/cites)", "default_allowed": True, "cost_label": "1"},
    "P10": {"label": "冲突复核(旧点, 已被 P22 取代)", "default_allowed": True, "cost_label": "1"},
}

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def gate_mode() -> str:
    """LLM_GATE_MODE(design/06 §4, T4.3): ask=默认/授权按点区分(默认);
    auto_allow=全部放行(实验); auto_deny=默认点也拒绝(开发/测试)。"""
    mode = (os.getenv("LLM_GATE_MODE", "ask") or "ask").strip().lower()
    return mode if mode in ("ask", "auto_allow", "auto_deny") else "ask"


def allowed(point_id: str, decision: str = "auto") -> bool:
    """某调用点此刻是否允许调 LLM(配合 LLM_GATE_MODE)。"""
    info = LLM_CALL_POINTS.get(point_id)
    if info is None:
        return False  # 未登记点一律不放行
    mode = gate_mode()
    if mode == "auto_allow":
        return True
    if mode == "auto_deny":
        return False  # 开发/测试: 一律零 LLM
    if decision == "allow":  # 用户显式授权(按钮点击)
        return True
    return bool(info["default_allowed"])  # ask 模式: 默认点放行, 授权点需 allow


def _gate_path() -> str:
    return os.getenv(
        "LLM_GATE_LOG",
        os.path.join(_BASE, "data", "logs", "llm_gate.jsonl"),
    )


def audit(point_id: str, student_id: str = "default", session_id: str = "",
          decision: str = "auto", ok: bool = True, note: str = "") -> bool:
    """记录一次 LLM 调用审计(design/06 §5 格式)。失败返回 False。"""
    info = LLM_CALL_POINTS.get(point_id)
    if info is None:
        point_id, decision = "unregistered", "auto"
    level = "authorized" if (info and not info["default_allowed"]) else "default"
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "student_id": student_id,
        "session_id": session_id,
        "point_id": point_id,
        "level": level,
        "decision": decision,
        "cost_label": (info or {}).get("cost_label", "1"),
        "result": "ok" if ok else "error",
        "note": note[:200],
    }
    try:
        path = _gate_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def counters() -> Dict:
    """审计汇总: {default, authorized, by_point: {P2: {...}}, unregistered}。"""
    out = {"default": 0, "authorized": 0, "by_point": {}, "unregistered": 0}
    path = _gate_path()
    if not os.path.exists(path):
        return out
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
                if rec.get("point_id") == "unregistered":
                    out["unregistered"] += 1
                    continue
                level = rec.get("level", "default")
                out[level] = out.get(level, 0) + 1
                pid = rec.get("point_id", "?")
                p = out["by_point"].setdefault(
                    pid, {"default": 0, "authorized": 0, "ok": 0, "error": 0})
                p[level] = p.get(level, 0) + 1
                p[rec.get("result", "ok")] = p.get(rec.get("result", "ok"), 0) + 1
    except OSError:
        pass
    return out
