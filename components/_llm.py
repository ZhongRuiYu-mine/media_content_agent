# coding: utf-8
"""LLM 调用工具（含容错重试）。

**openjiuwen 是可选的**：本模块在没装 LLM 运行时的机器上也要能 import 成功，
只是 `llm_runtime_available()` 返回 False、`call_llm()` 返回空串（调用方已有降级）。

分工：
  - 确定性链路（核验/缺口/成稿/渲染/预览/六角色辅助）→ **不经过这里**，零依赖；
  - 真调大模型（出选题/润色/合规/多平台改写/团队复盘）→ 才需要 openjiuwen + API Key。

调用失败做有限次重试；未配置模型时优雅降级返回空串，绝不把异常抛给主流程。
"""
import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_MAX_RETRIES = 2
_RETRY_DELAYS = [1.0, 3.0]  # 秒

SYSTEM_PROMPT = "你是新媒体编辑部的写作助手：只用素材里确有的信息，不改数字、人名、职务、时间、地点"


def llm_runtime_available() -> bool:
    """本机有没有装 LLM 运行时（openjiuwen）。没有就只能走确定性链路。"""
    try:
        from config import LLM_RUNTIME_AVAILABLE
    except Exception:  # noqa: BLE001
        return False
    return bool(LLM_RUNTIME_AVAILABLE)


def build_llm_config(agent_cfg):
    """构建 LLM 组件配置(内容域各授权点共用: 出选题/润色/合规/多平台改写)。

    没装运行时、或没配 Key 时返回 None —— 调用方据此降级，不必捕获异常。
    """
    if not llm_runtime_available():
        return None
    if not getattr(getattr(agent_cfg, "llm", None), "api_key", ""):
        return None                    # 没 Key 就不构造: 否则 openjiuwen 会直接抛校验错
    from openjiuwen.core.workflow import LLMCompConfig

    return LLMCompConfig(
        model_client_config=agent_cfg.llm.to_client_config(),
        model_config=agent_cfg.llm.to_request_config(),
        template_content=[{"role": "system", "content": SYSTEM_PROMPT}],
        response_format={"type": "json"},
        output_config={
            "type": "object",
            "description": "大模型输出",
            "properties": {"output": {"type": "string", "description": "输出内容"}},
            "required": ["output"],
        },
    )


async def call_llm(
    llm_config,
    system_prompt: str,
    user_prompt: str,
) -> str:
    """发起一次 LLM 对话，返回纯文本响应。失败重试，最终返回空串。"""
    if (
        llm_config is None
        or getattr(llm_config, "model_client_config", None) is None
        or getattr(llm_config, "model_config", None) is None
    ):
        return ""
    if not llm_runtime_available():
        return ""
    from openjiuwen.core.foundation.llm import Model, SystemMessage, UserMessage

    model = Model(llm_config.model_client_config, llm_config.model_config)
    messages = []
    if system_prompt:
        messages.append(SystemMessage(content=system_prompt))
    messages.append(UserMessage(content=user_prompt))

    last_err = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            response = await model.invoke(messages=messages)
            return getattr(response, "content", "") or ""
        except Exception as e:  # noqa: BLE001 - 有限重试，记录后继续
            last_err = e
            if attempt < _MAX_RETRIES:
                delay = _RETRY_DELAYS[attempt]
                logger.warning(
                    "LLM 调用失败(第%d次)，%gs 后重试: %s", attempt + 1, delay, e
                )
                await asyncio.sleep(delay)
            else:
                logger.error("LLM 调用最终失败: %s", e)
    return ""
