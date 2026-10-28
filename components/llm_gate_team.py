# coding: utf-8
"""components/llm_gate_team.py — 复盘智能体(swarm)的 LLM 审计 + 团队存储隔离。

把冒烟脚本验证过的两件事抽成可复用组件，复用项目既有 llm_gate（design/09 R7）：

1. register_llm_audit(): 幂等注册 openjiuwen callback 框架的 LLM 审计钩子。
   openjiuwen 的 ModelClient 在**每次** LLM 调用(invoke/stream)都触发一次
   LLM_OUTPUT(成功, 带 model_name/model_provider/usage/response) 或
   LLM_CALL_ERROR(失败, 带 error)。据此写"一次调用一条审计"进 llm_gate。
   只审计"团队会话内"的调用(读 openjiuwen.agent_teams.context 的 session_id)，
   避免与既有直接调用点(P2/P3/P6/P8)重复计数。

2. configure_team_home(base_dir): 把 openjiuwen 团队状态(home)指到指定目录，
   团队 DB / 工作区 / session 全部隔离到该目录，不碰 ~/.openjiuwen。
   （openjiuwen 用 team_name 在 home 下按团队分目录，本方法只需设一次全局 home。）

运行时机：复盘运行时启动时各调用一次即可（幂等）。
"""
from __future__ import annotations

import contextlib
import contextvars
import os
from typing import Dict

from components import llm_gate

# 复盘/HITS + HOTS 的 LLM 调用点（运行时登记到内存，不修改 llm_gate.py 文件）
_TEAM_CALL_POINTS = {
    "P11": {"label": "复盘协作(HITS/团队)", "default_allowed": True, "cost_label": "1"},
    "P12": {"label": "HOTS 重核验", "default_allowed": True, "cost_label": "1"},
    "P13": {"label": "HOTS 重新成段", "default_allowed": True, "cost_label": "1"},
}
for _pid, _info in _TEAM_CALL_POINTS.items():
    llm_gate.LLM_CALL_POINTS.setdefault(_pid, _info)

_registered = False
# 团队 session_id -> student_id（审计记录里写回学生，复盘运行时 bind）
_session_student: Dict[str, str] = {}
# 当前 LLM 调用点：默认 HITS(P11)；HOTS 执行期间用 hots_point() 切到 P12/P13
_POINT_CTX: contextvars.ContextVar[str] = contextvars.ContextVar(
    "llm_gate_team_point", default="P11")


@contextlib.contextmanager
def hots_point(point_id: str):
    """把当前(协程上下文内)的 LLM 审计点切到 point_id（HOTS: P12/P13）。"""
    token = _POINT_CTX.set(point_id)
    try:
        yield
    finally:
        _POINT_CTX.reset(token)


def bind_session_student(session_id: str, student_id: str) -> None:
    """把团队 session_id 关联到学生，审计记录用（复盘运行时创建会话时调用）。"""
    if session_id:
        _session_student[session_id] = student_id or "default"


def _usage_str(u) -> str:
    if not u:
        return "?"
    if isinstance(u, dict):
        keys = [k for k in ("total_tokens", "prompt_tokens", "completion_tokens") if k in u]
        if keys:
            return ",".join(f"{k}={u[k]}" for k in keys)
    return str(u)[:80]


def register_llm_audit(point_id: str = "P11") -> None:
    """幂等注册团队 LLM 审计钩子。只审计团队会话内的调用。"""
    global _registered
    if _registered:
        return

    from openjiuwen.agent_teams.context import get_session_id
    from openjiuwen.core.runner.callback import get_callback_framework
    from openjiuwen.core.runner.callback.events import LLMCallEvents

    _fw = get_callback_framework()

    @_fw.on(LLMCallEvents.LLM_OUTPUT)
    async def _audit_output(**kwargs):
        sid = get_session_id()
        if not sid:  # 非团队调用(既有 P2/P3/P6/P8 已就地审计)，跳过
            return
        model = kwargs.get("model_name") or "?"
        provider = kwargs.get("model_provider") or "?"
        resp = kwargs.get("response") or ""
        llm_gate.audit(_POINT_CTX.get(), student_id=_session_student.get(sid, "default"),
                       session_id=sid, decision="auto", ok=True,
                       note=f"model={model} provider={provider} usage[{_usage_str(kwargs.get('usage'))}] chars={len(str(resp))}")

    @_fw.on(LLMCallEvents.LLM_CALL_ERROR)
    async def _audit_error(**kwargs):
        sid = get_session_id()
        if not sid:
            return
        model = kwargs.get("model_name") or "?"
        provider = kwargs.get("model_provider") or "?"
        err = kwargs.get("error")
        llm_gate.audit(_POINT_CTX.get(), student_id=_session_student.get(sid, "default"),
                       session_id=sid, decision="auto", ok=False,
                       note=f"model={model} provider={provider} err={type(err).__name__}:{str(err)[:120]}")

    _registered = True


def configure_team_home(base_dir: str) -> str:
    """把 openjiuwen 团队状态(home)指到 base_dir，返回该路径（幂等，只设一次全局）。"""
    from openjiuwen.agent_teams.paths import configure_openjiuwen_home

    path = os.path.abspath(base_dir)
    os.makedirs(path, exist_ok=True)
    configure_openjiuwen_home(path)
    return path


__all__ = ["bind_session_student", "configure_team_home", "hots_point", "register_llm_audit"]
