# coding: utf-8
"""components/agents/hots.py — HOTS 指挥群(design/11 §3.2: 总编模式)。

用户在指挥位下一句话, 一次性 WORKER 并行执行(用完即弃, 复盘级上下文):

- `re_verify`(原 `re_score` → 重核验): 并行 2 个 worker —— **事实核验**(逐条断言是否
  在素材里有依据) + **合规风险复核**(隐私/未证实承诺/争议表述);
- `re_draft`(原 `re_question` → 重新成段): 1 个 worker 按"缺口清单 + 素材事实"
  重新成段, 并给出**多平台版本**(订阅号/短视频脚本/海报文案)。

脚本是静态模板(payload 走 run(args), 不插值进源码), 由 review_runtime 经
materialize_swarmflow_script + run_swarmflow 执行。

**边界**: 两个命令都不许替用户认定事实真伪 —— 核验只给"素材里有没有依据",
多平台改写必须过断言级 diff(事实、数字、人名、时间、地点一律照抄)。
"""
from __future__ import annotations

RE_VERIFY_SCRIPT = '''META = {
    "name": "hots_re_verify",
    "description": "对当前稿件重新做事实核验与合规风险复核, 给出可核对的证据与改法",
}

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "description": "逐条断言核验结果",
            "items": {
                "type": "object",
                "properties": {
                    "quote": {"type": "string", "description": "稿件里的原句"},
                    "verdict": {"type": "string", "description": "有依据 | 说法不一 | 查不到"},
                    "evidence": {"type": "string", "description": "依据的素材 eid 或说明"},
                },
                "required": ["quote", "verdict"],
            },
        },
        "summary": {"type": "string", "description": "一句话结论(只描述证据状况, 不下审核结论)"},
    },
    "required": ["claims"],
}

COMPLIANCE_SCHEMA = {
    "type": "object",
    "properties": {
        "risks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "description": "隐私 | 未证实承诺 | 争议表述 | 往届信息当今年用"},
                    "quote": {"type": "string", "description": "原文片段"},
                    "advice": {"type": "string", "description": "建议改法"},
                },
                "required": ["kind", "quote", "advice"],
            },
        },
        "summary": {"type": "string", "description": "风险概览(提示用, 不是审核结论)"},
    },
    "required": ["risks"],
}

async def run(args):
    from swarmflow import agent, parallel

    a = args or {}
    draft = a.get("draft") or ""
    check_list = a.get("check_list") or ""
    materials = a.get("materials") or ""

    def verify_thunk():
        return agent(
            "你是事实核查员, 只输出 JSON。逐条核对稿件里的数字/人名/职务/时间/地点"
            "能否在素材里找到依据; 分成「有依据 / 说法不一 / 查不到」三类。"
            "查不到的必须明确说查不到, 不许替素材编内容, 也不要下审核结论。"
            "\\n【稿件】\\n" + draft + "\\n【待核实清单(系统确定性检测结果)】\\n" + check_list +
            "\\n【素材库摘要】\\n" + materials,
            label="verify", phase="事实核验", schema=VERIFY_SCHEMA)

    def compliance_thunk():
        return agent(
            "你是校园宣传合规风险审查员, 只输出 JSON。审查稿件是否含有: 个人信息(电话/住址/学号)、"
            "未证实的效果承诺、贬损或比较性表述、往届信息被当今年事实使用。"
            "每条给出原文片段与建议改法; 只提示风险, 不下审核结论。"
            "\\n【稿件】\\n" + draft + "\\n【素材库摘要】\\n" + materials,
            label="compliance", phase="合规复核", schema=COMPLIANCE_SCHEMA)

    verify, compliance = await parallel([verify_thunk, compliance_thunk])
    return {"verify": verify, "compliance": compliance}
'''

RE_DRAFT_SCRIPT = '''META = {
    "name": "hots_re_draft",
    "description": "按缺口清单与素材事实重新成段, 并一次产出多平台版本(总编模式)",
}

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "segments": {
            "type": "array",
            "description": "段结构: kind ∈ lead/background/process/highlight/impact/closing",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string"},
                    "text": {"type": "string", "description": "段正文(只能用素材里的事实)"},
                    "source_eids": {"type": "array", "items": {"type": "string"},
                                    "description": "这一段用了哪几条素材"},
                },
                "required": ["kind", "text"],
            },
        },
        "need_verify": {
            "type": "array", "items": {"type": "string"},
            "description": "缺依据、需要人去核实的表述(不许写进正文当事实)",
        },
        "platforms": {
            "type": "object",
            "description": "多平台版本: {wechat: 文本, shortvideo: 脚本, poster: 文案}",
        },
    },
    "required": ["segments", "need_verify"],
}

async def run(args):
    from swarmflow import agent

    a = args or {}
    topic = a.get("topic") or ""
    check_list = a.get("check_list") or ""
    materials = a.get("materials") or ""
    draft = a.get("draft") or ""

    return await agent(
        "你是社团媒体部总编, 只输出 JSON。围绕「" + topic + "」重新成段, 并产出多平台版本。"
        "硬约束: 只能用素材里已有的事实; 缺依据的表述写进 need_verify, 不许写进正文; "
        "数字/人名/职务/时间/地点一律照抄素材, 不许改。"
        "\\n【现有稿件】\\n" + draft + "\\n【待核实清单】\\n" + check_list +
        "\\n【可用素材】\\n" + materials,
        label="draft", phase="重新成段", schema=DRAFT_SCHEMA)
'''

SCRIPTS = {
    "re_verify": RE_VERIFY_SCRIPT,
    "re_draft": RE_DRAFT_SCRIPT,
    # 兼容旧命令名(旧调用点仍可能用)
    "re_score": RE_VERIFY_SCRIPT,
    "re_question": RE_DRAFT_SCRIPT,
}


def script_for(command: str):
    """取命令对应的 swarmflow 脚本源码; 未知命令返回 None。"""
    return SCRIPTS.get(command)


__all__ = ["script_for", "SCRIPTS"]
