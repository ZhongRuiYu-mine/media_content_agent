# coding: utf-8
"""check_env.py — 这台机器能跑到哪一档（交付/答辩前先跑这一条）。

它只回答一件事：**现在这个环境，哪些功能能用、缺什么、怎么补**。
不联网、不写库、不需要 API Key。

用法（在 deliver/ 下）:

    D:\\jiuwen\\Scripts\\python.exe tools\\check_env.py
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _HERE)

OK = "可用"
NO = "不可用"


def _mark(ok: bool) -> str:
    return OK if ok else NO


def main() -> int:
    print("=" * 72)
    print("环境自检 · 新媒体信宣智能体（采编台）")
    print("=" * 72)

    print("\n[1] 运行时")
    print("  python      :", sys.version.split()[0], "|", sys.executable)
    try:
        import config
        print("  openjiuwen  :", _mark(config.LLM_RUNTIME_AVAILABLE),
              "（大模型/团队路线的运行时；缺了只走确定性）")
    except Exception as e:  # noqa: BLE001
        print("  openjiuwen  : 检查失败:", e)
        return 1

    print("\n[2] 能力分档")
    from server import capabilities
    caps = capabilities()
    label = {"core": "① 核心（必装）",
             "llm": "② 大模型 / 团队复盘",
             "embeddings": "③ 语义检索",
             "mongodb": "③ 选题库(MongoDB)"}
    for key, info in caps.items():
        print("  %-22s %-4s  需要: %s" % (label.get(key, key), _mark(info.get("ok")),
                                          info.get("needs")))
    if not caps.get("core", {}).get("ok"):
        print("\n  !! 核心档不可用 —— 先解决 core，其他都是可选")
        return 1

    print("\n[3] 闸门与配置")
    from components import llm_gate
    mode = llm_gate.gate_mode()
    print("  LLM_GATE_MODE :", mode,
          "（auto_deny=全拒，演示零 LLM 就用它；ask=默认点放行、授权点要用户点）")
    print("  EMBEDDING_OFF :", (os.getenv("EMBEDDING_OFF") or "(未设)").strip(),
          "（置 1 表示不加载任何向量模型）")
    print("  API Key       :", "已配(env)" if os.getenv("API_KEY") else "未配(界面「设置」里也能配)")

    print("\n[4] 结论")
    if caps.get("llm", {}).get("ok"):
        print("  这一档能演**全部**内容：确定性链路 + 团队复盘（团队还需在设置里填 Key）。")
    else:
        print("  这一档能演**核心全部**：核验/缺口/成稿/三渠道预览/六角色确定性辅助/审核记录。")
        print("  六角色团队路线不可用 → 界面会明说原因并自动降级，不影响演示主线。")
    print("  （详细三档说明见 交付说明.md）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
