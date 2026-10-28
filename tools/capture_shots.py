# coding: utf-8
"""capture_shots.py — 一键截「实机界面图」（不是示意图，是真的把工作台跑起来截图）。

为什么要有这个脚本：配图要能复现。手工点十几次再截，下次就对不上了。
这个脚本做四件事：

  1. 用**临时数据目录**起一个自己的 server（不动你 data/ 里的演示数据）；
  2. 灌一份演示语料 + 让系统先出一版稿（截出来的界面上有东西可看）；
  3. 用 Edge 无头模式逐个场景截图（场景靠页面自带的 `?scene=` 参数，只调界面已有动作）；
  4. 收尾：关掉 server、删临时目录。

用法（在 deliver/ 下）：

    D:\\jiuwen\\Scripts\\python.exe tools\\capture_shots.py
    D:\\jiuwen\\Scripts\\python.exe tools\\capture_shots.py --only stack,assist
    D:\\jiuwen\\Scripts\\python.exe tools\\capture_shots.py --width 1680 --height 1050

产出：figures/shots/*.png（1600×1000 起，`--scale 2` 可出 2 倍高清）。

注意（踩过的坑）：
  - Edge 无头模式在本机沙箱下**会失败**（mojo 命名管道被拒），必须在放开文件/进程
    权限的环境里跑；脚本会明确报这个错，而不是给你一张半截图。
  - 截图脚本自己起 server，所以端口冲突时用 --port 换一个，别去抢 8080。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # deliver/
_TMP = os.path.join(_HERE, "_shots_tmp")
OUT_DIR = os.path.join(_HERE, "figures", "shots")
TOKEN = "shot-token"

EDGE_CANDIDATES = [
    os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
    os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
    os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe"),
    os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
]

# 两个独立工作区: 场景之间不互相污染(assist 会往稿子里拖素材)
CLUBS = ["shot_a", "shot_b"]

# (输出文件名, 场景参数, 说明) —— 一个阶段 1~2 张
SCENES = [
    ("01-选题-待核实清单", "club=shot_a&scene=checklist", "五类缺口并列；矛盾项摆两版，谁为准由人点"),
    ("02-原型-三列总览", "club=shot_a&scene=stack", "左素材池 / 中组装 / 右辅助与预览，一屏三列"),
    ("03-原型-命令面板", "club=shot_a&scene=palette", "低频动作收进 ⌘K，顶栏只留两个按钮"),
    ("04-实现-积木流与预览", "club=shot_b&scene=stack", "拖进来的块先到在上；下方三渠道字数实时在算"),
    ("05-实现-六角色辅助", "club=shot_b&scene=assist", "把含隐私的素材拖进来，角色给出「应用：脱敏」"),
    ("06-测试-仪表盘", "club=shot_a&scene=dashboard", "确定性操作 vs LLM 调用，可当场对数"),
    ("07-测试-审核记录", "club=shot_a&scene=record", "给审稿人的证据与留痕（不是审核结论）"),
    ("08-打磨-设置", "club=shot_a&scene=settings", "账号 / API 配置 / 外观，一处收口"),
    ("09-打磨-浅色主题", "club=shot_b&scene=stack&theme=light", "同一界面的浅色主题（另有纸质主题）"),
]


def pick_port(start: int) -> int:
    """从 start 起找一个没人监听的口。绝不复用别人的服务: 否则截到的可能是旧进程。"""
    import socket
    for port in range(start, start + 40):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("找不到空闲端口")


def ours(port: int) -> bool:
    """确认端口上跑的是**我们这个** server(令牌对得上), 而不是别人的残留进程。"""
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/content/state?club_id=__probe__",
                                 headers={"X-Auth-Token": TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        return e.code != 401          # 401 = 令牌不对, 说明是别人的服务
    except (urllib.error.URLError, OSError):
        return False


def find_browser() -> str:
    for p in EDGE_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def wait_health(port: int, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/api/health"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    return False


def post(port: int, path: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"X-Auth-Token": TOKEN, "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:200]}"}


def shoot(browser: str, port: int, name: str, query: str, size: str, scale: int) -> bool:
    out = os.path.join(OUT_DIR, name + ".png")
    if os.path.exists(out):
        os.remove(out)
    url = f"http://127.0.0.1:{port}/workbench?{query}"
    profile = os.path.join(_TMP, "edge-profile")
    cmd = [
        browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
        "--no-first-run", "--no-default-browser-check", "--disable-crash-reporter",
        f"--user-data-dir={profile}",
        f"--window-size={size}",
        f"--force-device-scale-factor={scale}",
        "--virtual-time-budget=15000",
        f"--screenshot={out}",
        url,
    ]
    p = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="ignore", timeout=180)
    ok = os.path.exists(out) and os.path.getsize(out) > 20000
    if not ok:
        tail = (p.stderr or "")[-400:]
        if "platform_channel" in tail or "OpenProcess" in tail or "拒绝访问" in tail:
            print("    !! Edge 被沙箱拦住(mojo/OpenProcess 拒绝访问) —— "
                  "请在放开进程权限的环境里跑本脚本")
        else:
            print("    !! 没生成图: " + (tail.replace("\n", " ")[:220] or "无输出"))
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="只截其中几个场景(逗号分隔的序号或名字片段)")
    ap.add_argument("--port", type=int, default=8098)
    ap.add_argument("--width", type=int, default=1600)
    ap.add_argument("--height", type=int, default=1000)
    ap.add_argument("--scale", type=int, default=1, help="2 = 两倍高清")
    args = ap.parse_args()

    browser = find_browser()
    if not browser:
        print("找不到 Edge / Chrome，无法截实机图。请装 Edge 或改 EDGE_CANDIDATES。")
        return 2
    print("浏览器:", browser)

    shutil.rmtree(_TMP, ignore_errors=True)
    os.makedirs(os.path.join(_TMP, "graphs"), exist_ok=True)
    os.makedirs(OUT_DIR, exist_ok=True)

    env = dict(os.environ)
    env.update({
        "PYTHONIOENCODING": "utf-8",
        "WEB_PORT": str(args.port),
        "AUTH_TOKEN": TOKEN,
        "EMBEDDING_OFF": "1",
        "LLM_GATE_MODE": "auto_deny",      # 截图不依赖 LLM: 团队路线会落到确定性版本
        "STORAGE_BASE_DIR": os.path.join(_TMP, "graphs"),
        "CONTENT_VERSIONS_DIR": os.path.join(_TMP, "versions"),
        "TOPIC_BANK_DIR": os.path.join(_TMP, "topics"),
        "REPORT_DIR": os.path.join(_TMP, "reports"),
        "RESOLUTION_DIR": os.path.join(_TMP, "resolutions"),
        "USERS_FILE": os.path.join(_TMP, "users.json"),
        "LLM_GATE_LOG": os.path.join(_TMP, "llm_gate.jsonl"),
    })
    port = pick_port(args.port)
    env["WEB_PORT"] = str(port)
    server = subprocess.Popen([sys.executable, "server.py"], cwd=_HERE, env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    got = 0
    try:
        if not wait_health(port):
            print("服务没起来(端口 %d)" % port)
            return 1
        if not ours(port):
            print("端口 %d 上的服务不是本脚本起的(令牌对不上), 换端口重试" % port)
            return 1
        print("服务已就绪: http://127.0.0.1:%d/workbench" % port)
        for club in CLUBS:
            seeded = post(port, "/api/content/seed_demo", {"club_id": club})
            drafted = post(port, "/api/content/draft",
                           {"club_id": club, "max_segments": 6})
            segs = len(((drafted or {}).get("draft") or {}).get("segments") or [])
            print(f"工作区 {club}: 语料 {seeded.get('count', seeded)} 条, 初稿 {segs} 块")

        only = [x.strip() for x in args.only.split(",") if x.strip()]
        size = f"{args.width},{args.height}"
        for name, query, desc in SCENES:
            if only and not any(o in name for o in only):
                continue
            print(f"  -> {name}.png  ({desc})")
            if shoot(browser, port, name, query, size, args.scale):
                got += 1
    finally:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(server.pid)],
                           capture_output=True, timeout=30)
        except Exception:  # noqa: BLE001
            server.kill()
        time.sleep(1)
        shutil.rmtree(_TMP, ignore_errors=True)

    print()
    print(f"完成 {got} 张 -> {OUT_DIR}")
    if got:
        print("这些是**真机截图**（跑起来的工作台），不是示意图；"
              "改了界面重跑本脚本即可更新。")
    return 0 if got else 1


if __name__ == "__main__":
    sys.exit(main())
