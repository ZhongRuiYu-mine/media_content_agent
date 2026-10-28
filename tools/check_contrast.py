# coding: utf-8
"""tools/check_contrast.py — 颜色令牌对比度校验（WCAG 口径）。

深色主题最容易翻车的地方不是"丑"，是"看不清"：正文灰过头、次要文字飘在背景里、
徽章色和底色贴在一起。这个脚本把 tokens.css 里的令牌两两算对比度，
把"护眼"变成可验证的数字，而不是审美争论。

口径（design 需求：长时间阅读不疲惫 + 深色护眼）：
    正文（--txt-1 on 各层底色）      ≥ 7.0   （WCAG AAA 正文）
    次要文字（--txt-2）              ≥ 4.5   （AA 正文 / AAA 大字）
    弱提示（--txt-3）                ≥ 3.5   （大字号或非关键信息）
    语义色（--c-* on 自己的底色）     ≥ 4.5   （徽章、标签、状态字）
    强调色（--acc on 底色/其自身底）  ≥ 3.0   （大字或图标；按钮上另有 --txt-on-acc）
    按钮前景（--txt-on-acc on --acc） ≥ 4.5

用法:
    D:\\jiuwen\\Scripts\\python.exe tools\\check_contrast.py
退出码 0 = 全部达标; 1 = 有不达标项(会打印具体数值与建议)。
"""
from __future__ import annotations

import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_TOKENS = os.path.join(_ROOT, "static", "tokens.css")

FAILS = []


def srgb_to_lin(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * srgb_to_lin(r) + 0.7152 * srgb_to_lin(g) + 0.0722 * srgb_to_lin(b)


def contrast(fg: str, bg: str) -> float:
    l1, l2 = luminance(fg), luminance(bg)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def parse_themes(css: str) -> dict:
    """从 tokens.css 抽出 {主题: {令牌: 值}}（只取 #rrggbb 这类直接色值）。"""
    themes: dict = {}
    # 匹配 `:root` / `:root[data-theme="x"]` 选择器块
    for m in re.finditer(r"(?m)^([^@\n][^{]*?)\{([^}]*)\}", css):
        sel, body = m.group(1).strip(), m.group(2)
        names = []
        if re.search(r":root(?!\[)", sel) or sel == ":root,":
            names.append("dark")            # 默认块 = 深色
        for t in re.findall(r'data-theme="([a-z]+)"', sel):
            names.append(t)
        if not names:
            continue
        for name in names:
            bucket = themes.setdefault(name, {})
            for dm in re.finditer(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", body):
                val = dm.group(2).strip()
                if re.fullmatch(r"#[0-9a-fA-F]{3,8}", val):
                    bucket[dm.group(1)] = val
    return themes


def report(theme: str, label: str, fg: str, bg: str, need: float,
           fg_name: str, bg_name: str) -> None:
    got = contrast(fg, bg)
    ok = got >= need
    mark = "ok  " if ok else "FAIL"
    line = f"  {mark} [{theme}] {label}: {got:.2f} (需 ≥ {need})  {fg_name} on {bg_name}"
    print(line)
    if not ok:
        FAILS.append(f"[{theme}] {label} {got:.2f} < {need}")


def main() -> int:
    with open(_TOKENS, "r", encoding="utf-8") as f:
        css = f.read()
    themes = parse_themes(css)
    if not themes:
        print("没解析出任何主题令牌，检查 tokens.css 的写法")
        return 1

    surfaces = ["--bg-base", "--bg-surface", "--bg-raised", "--bg-sunken",
                "--bg-hover", "--bg-active"]
    print("=" * 74)
    print("颜色令牌对比度校验（WCAG AAA 正文 7:1 / AA 4.5:1）")
    print("=" * 74)

    for theme in ("dark", "light", "paper"):
        tk = themes.get(theme)
        if not tk:
            print(f"\n[{theme}] 主题缺失")
            FAILS.append(f"主题缺失: {theme}")
            continue
        print(f"\n[{theme}] 正文与底色（每层底都要能读）")
        for s in surfaces:
            if s in tk:
                report(theme, "正文", tk["--txt-1"], tk[s], 7.0, "--txt-1", s)
        print(f"[{theme}] 次要文字与弱提示（取最亮的底色做最坏情况）")
        brightest = max((tk[s] for s in surfaces if s in tk), key=luminance) \
            if theme != "dark" else max((tk[s] for s in surfaces if s in tk), key=luminance)
        report(theme, "次要文字", tk["--txt-2"], brightest, 4.5, "--txt-2", "最亮底色")
        report(theme, "弱提示", tk["--txt-3"], brightest, 3.5, "--txt-3", "最亮底色")

        print(f"[{theme}] 语义色与各自的浅底（徽章/标签可读性）")
        for base in ("cite", "block", "warn", "ok", "resolve", "info"):
            fg, bg = tk.get(f"--c-{base}"), tk.get(f"--c-{base}-bg")
            if fg and bg:
                report(theme, f"语义 {base}", fg, bg, 4.5, f"--c-{base}", f"--c-{base}-bg")

        print(f"[{theme}] 强调色与权威度徽章")
        report(theme, "强调色", tk["--acc"], tk["--bg-surface"], 3.0, "--acc", "--bg-surface")
        report(theme, "按钮前景", tk["--txt-on-acc"], tk["--acc"], 4.5, "--txt-on-acc", "--acc")
        for a in ("official", "organizer", "onsite", "member", "hearsay"):
            key = f"--auth-{a}"
            if key in tk:
                report(theme, f"权威度 {a}", tk[key], tk["--bg-sunken"], 4.5,
                       key, "--bg-sunken")

    print("\n" + "=" * 74)
    if FAILS:
        print(f"不达标 {len(FAILS)} 项：")
        for x in FAILS:
            print("  - " + x)
        print("建议：深色主题里把该令牌往亮处调一档（或把底色压暗一档），改 tokens.css 即可。")
        return 1
    print("全部达标：正文 ≥7:1，次要/徽章 ≥4.5:1，弱提示 ≥3.5:1。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
