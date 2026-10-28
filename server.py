# coding: utf-8
"""server.py — 新媒体信宣智能体 · 采编台服务(内容域)。

只暴露内容域(采编台)需要的接口:

  页面      /  与 /workbench(同一个工作台)
  素材      /api/content/seed_demo|analyze|ingest|state|gaps
  采编      /api/content/topics|draft|verify|platforms|preview|assist
  留痕      /api/content/review_record|review_export|export_package|dashboard|versions|rollback
  六角色    /api/content/review(团队多智能体) + /api/review/*
  账号      /api/auth/*  /api/user/llm*

鉴权两层: 进程令牌 X-Auth-Token(防跨站调用) + 用户令牌 X-User-Token(谁登录了)。
"""
from __future__ import annotations

import json
import logging
import hmac
import os
import secrets
from typing import Dict

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

# 工作流执行超时调大
os.environ.setdefault("WORKFLOW_EXECUTE_TIMEOUT", "1800")

from components.review_runtime import get_review_runtime  # ← 采编复盘团队(design/11 §9)


def _resolve_agent_config(key: str):
    """按 key(工作区/账号)解析 LLM 配置: 用户配置优先, 缺省回落 .env。"""
    from config import build_agent_config
    return build_agent_config(_user_configs.get(key) or {})

logger = logging.getLogger(__name__)

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_USER_CONFIGS_PATH = os.path.join(_DATA_DIR, "user_configs.json")

# 存储用户配置
_user_configs: Dict[str, Dict] = {}

# ---------- 安全: 进程令牌鉴权(本地服务防跨站脚本/恶意网页/CSRF) ----------
# 每次进程启动生成随机令牌, 注入首页内联脚本; 除 /api/health 外所有 /api/* 需带
# X-Auth-Token 头(SSE 不支持自定义头, /api/review/stream 改走 query token)。
# AUTH_TOKEN 可用环境变量固定(测试/多进程共享); AUTH_ENABLED=0 关闭鉴权(仅测试用)。
_AUTH_TOKEN = os.getenv("AUTH_TOKEN") or secrets.token_urlsafe(32)
_AUTH_EXEMPT = {"/api/health"}


def _auth_ok(request: Request) -> bool:
    # AUTH_ENABLED 每次请求动态读取(测试内可切换)
    if os.getenv("AUTH_ENABLED", "1").strip().lower() in ("0", "false", "no"):
        return True
    path = request.url.path
    if not path.startswith("/api/") or path in _AUTH_EXEMPT or request.method == "OPTIONS":
        return True
    token = request.headers.get("x-auth-token", "")
    if path == "/api/review/stream":
        token = token or request.query_params.get("token", "")
    return bool(token) and hmac.compare_digest(token, _AUTH_TOKEN)


# ---------- 用户配置管理 ----------
async def load_user_configs():
    """从文件加载用户配置"""
    global _user_configs
    if os.path.exists(_USER_CONFIGS_PATH):
        try:
            with open(_USER_CONFIGS_PATH, "r", encoding="utf-8") as f:
                _user_configs = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"加载用户配置失败: {e}")
            _user_configs = {}
    else:
        _user_configs = {}


async def save_user_configs():
    """保存用户配置到文件"""
    os.makedirs(os.path.dirname(_USER_CONFIGS_PATH), exist_ok=True)
    tmp = _USER_CONFIGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_user_configs, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _USER_CONFIGS_PATH)


async def get_user_config(student_id: str) -> Dict:
    """获取用户配置"""
    return _user_configs.get(student_id, {})


# ---------- HTTP 接口 ----------
async def index(request: Request) -> Response:
    """首页 = 三栏工作台(design/11 §7)。

    根路径直接交给工作台(没有第二套界面),
    省一次跳转; 老书签仍可用。
    """
    return await workbench(request)


async def workbench(request: Request) -> Response:
    """三栏工作台(design/11 §7): 素材库 — 稿件候选 — 多平台预览。

    进程令牌写进页面内联脚本(每次启动随机), 前端所有 /api 请求自动带上。
    """
    with open(os.path.join(_STATIC_DIR, "workbench.html"), "r", encoding="utf-8") as f:
        html = f.read().replace("__AUTH_TOKEN__", _AUTH_TOKEN)
    return Response(html, media_type="text/html")


async def api_health(request: Request) -> Response:
    return JSONResponse({"status": "ok"})


async def api_config(request: Request) -> Response:
    """GET /api/config → 模型端点 + 内容域旋钮 + **能力清单**。

    capabilities 给交付方自检用: 这台机器哪一档能力可用、缺什么。确定性链路
    (核验/缺口/成稿/渲染/三渠道预览/六角色辅助/账号)永远可用, 只依赖 starlette。
    """
    from config import DEFAULT_CONFIG, LLM_RUNTIME_AVAILABLE
    from content.platform_spec import PLATFORM_SPECS, platforms
    from content.content_engine import MIN_CLAUSE_CHARS, MIN_SEGMENT_MATERIALS
    cfg = DEFAULT_CONFIG
    return JSONResponse(
        {
            "model_name": cfg.llm.model_name,
            "provider": cfg.llm.provider,
            "api_base": cfg.llm.api_base,
            "api_key_set": bool(cfg.llm.api_key),
            "llm_runtime_installed": bool(LLM_RUNTIME_AVAILABLE),
            "capabilities": capabilities(),
            # 内容域真实旋钮(env 可覆盖): 演示/答辩时能当场指认
            "engine": {
                "min_clause_chars": MIN_CLAUSE_CHARS,
                "min_segment_materials": MIN_SEGMENT_MATERIALS,
                "platforms": list(platforms()),
                "platform_limits": {k: v["max_chars"] for k, v in PLATFORM_SPECS.items()},
            },
        }
    )


def capabilities() -> dict:
    """这台机器上有哪些能力可用(交付自检与答辩说明共用一份口径)。"""
    from config import LLM_RUNTIME_AVAILABLE
    return {
        "core": {"ok": True, "needs": "starlette + uvicorn"},
        "llm": {"ok": bool(LLM_RUNTIME_AVAILABLE),
                "needs": "openjiuwen 运行时 + API Key（不装就只走确定性）"},
        "embeddings": {"ok": _embedding_ready(),
                       "needs": "EMBEDDING_BASE_URL 或本地模型（可选，默认关闭）"},
        "mongodb": {"ok": _mongo_ready(),
                    "needs": "MONGODB_URI（可选，不可用即文件兜底）"},
    }


def _embedding_ready() -> bool:
    try:
        from components.embedding import create_embedding_from_env
        return bool(create_embedding_from_env().available())
    except Exception:  # noqa: BLE001
        return False


def _mongo_ready() -> bool:
    try:
        from components.mongo_store import mongo_available
        return bool(mongo_available())
    except Exception:  # noqa: BLE001
        return False


# ---------- 账号(右上角「设置」): 注册 / 登录 / 退出 ----------
# 与进程令牌 X-Auth-Token 是两层: 进程令牌防跨站调用, 用户令牌回答"谁登录了"。
# 用户令牌走 X-User-Token 头(SSE 之类不支持自定义头的场景不需要它)。
def _user_token(request: Request) -> str:
    return request.headers.get("x-user-token", "")


def _current_user(request: Request):
    from components import users
    return users.session_user(_user_token(request))


def _masked(key: str) -> str:
    if not key:
        return ""
    return f"****{key[-4:]}" if len(key) > 4 else "****"


async def api_auth_register(request: Request) -> Response:
    """POST: {username, password} → 注册并自动登录, 返回用户令牌。"""
    from components import users
    try:
        body = await request.json()
        ok, res = users.register(body.get("username") or "", body.get("password") or "")
        return JSONResponse({"ok": ok, **res}, status_code=200 if ok else 400)
    except Exception as e:
        logger.exception("注册失败")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


async def api_auth_login(request: Request) -> Response:
    """POST: {username, password} → 登录, 返回用户令牌。"""
    from components import users
    try:
        body = await request.json()
        ok, res = users.login(body.get("username") or "", body.get("password") or "")
        return JSONResponse({"ok": ok, **res}, status_code=200 if ok else 401)
    except Exception as e:
        logger.exception("登录失败")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


async def api_auth_logout(request: Request) -> Response:
    """POST → 退出登录(作废用户令牌)。"""
    from components import users
    return JSONResponse({"ok": users.logout(_user_token(request))})


async def api_auth_me(request: Request) -> Response:
    """GET → 当前登录用户(未登录返回 ok=false, 不报错: 匿名也能用工作台)。"""
    from components import users
    name = _current_user(request)
    return JSONResponse({"ok": bool(name), "username": name or "",
                         "user_count": users.user_count()})


async def api_user_llm_get(request: Request) -> Response:
    """GET → 当前用户的 LLM 配置(Key 只回掩码)。未登录时回的是全局默认是否可用。"""
    name = _current_user(request)
    cfg = _user_configs.get(name or "", {}) if name else {}
    from config import DEFAULT_CONFIG
    dflt = DEFAULT_CONFIG.llm
    key = cfg.get("api_key") or ""
    return JSONResponse({
        "ok": True,
        "username": name or "",
        "logged_in": bool(name),
        "configured": bool(key),
        "api_key_masked": _masked(key),
        "api_base": cfg.get("api_base") or dflt.api_base,
        "model_name": cfg.get("model_name") or dflt.model_name,
        "provider": cfg.get("provider") or dflt.provider,
        "fallback_available": bool(getattr(dflt, "api_key", "")),
        "note": "没有 Key 也能用: 六个角色与三渠道预览都是确定性实现, 会自动降级。",
    })


async def api_user_llm_set(request: Request) -> Response:
    """POST: {api_key?, api_base?, model_name?, provider?, clear?} → 存当前用户的 LLM 配置。

    存在 `data/user_configs.json`, 键是登录账号; 同时同步进全局 Runtime, 让内容域的
    LLM 授权点(润色/合规/多平台改写)用当前登录用户的 Key。
    """
    name = _current_user(request)
    if not name:
        return JSONResponse({"ok": False, "error": "先登录再配置 API"}, status_code=401)
    try:
        body = await request.json()
        cfg = _user_configs.setdefault(name, {})
        if body.get("clear"):
            for k in ("api_key", "api_base", "model_name", "provider"):
                cfg.pop(k, None)
        else:
            for k in ("api_key", "api_base", "model_name", "provider"):
                if body.get(k) not in (None, ""):
                    cfg[k] = body[k]
        await save_user_configs()
        return JSONResponse({"ok": True, "configured": bool(cfg.get("api_key")),
                             "api_key_masked": _masked(cfg.get("api_key") or "")})
    except Exception as e:
        logger.exception("保存用户 LLM 配置失败")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


async def api_user_llm_test(request: Request) -> Response:
    """POST → 用当前用户的配置真发一次最小请求, 报通不通。

    这不是"编辑动作", 所以不过 LLM 闸门(闸门管的是会写进稿件的调用);
    它只是把配置错误在界面上暴露出来, 免得用户以为"没 Key 也能用"是出了故障。
    """
    import asyncio
    name = _current_user(request)
    cfg = dict(_user_configs.get(name or "", {})) if name else {}
    if not cfg.get("api_key"):
        return JSONResponse({"ok": False, "error": "还没填 API Key"})
    from components._llm import build_llm_config, call_llm
    from config import build_agent_config
    agent_cfg = build_agent_config(cfg)
    try:
        text = await asyncio.wait_for(
            call_llm(build_llm_config(agent_cfg),
                     '你是连通性检测工具。只输出 JSON: {"output": "你好"}',
                     "连通性测试"), 25)
        return JSONResponse({"ok": bool(text), "reply": (text or "")[:60],
                             "model": agent_cfg.llm.model_name})
    except asyncio.TimeoutError:
        return JSONResponse({"ok": False, "error": "25 秒没有响应（检查 api_base 与网络）"})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"})


def _apply_user_llm(request: Request, key: str) -> None:
    """把当前登录用户的 LLM 配置挂到指定工作区(内容域按 club_id 取配置)。

    登录用户配的 Key 应该对自己的稿件生效 —— 内容域的 LLM 授权点都按 club_id 取
    `resolve_agent_config`, 所以这里把用户配置映射过去。没登录/没填 Key 就不动,
    保持原有的环境变量默认值。
    """
    name = _current_user(request)
    if not name:
        return
    src = _user_configs.get(name) or {}
    if not src.get("api_key"):
        return
    dst = _user_configs.setdefault(key, {})
    for k in ("api_key", "api_base", "model_name", "provider"):
        if src.get(k):
            dst[k] = src[k]


def _content_rt():
    """内容域运行时(design/11 §5.5 六步两相)。"""
    from content.content_runtime import get_content_runtime
    return get_content_runtime()


async def api_content_analyze(request: Request) -> Response:
    """POST: {club_id, text} → 素材逐句裁决(① 素材录入: 切句 + 内容域裁决矩阵)。"""
    try:
        body = await request.json()
        return JSONResponse(await _content_rt().analyze_text(
            body.get("club_id", "default"), body.get("text", "")))
    except Exception as e:
        logger.exception("content analyze 失败")
        return JSONResponse({"error": str(e)}, status_code=400)


async def api_content_ingest(request: Request) -> Response:
    """POST: {club_id, materials:[{content, source, type?, subject?, period?}]} → 素材入库。"""
    try:
        body = await request.json()
        return JSONResponse(_content_rt().ingest(
            body.get("club_id", "default"), body.get("materials") or [],
            session_id=body.get("session_id", "")))
    except Exception as e:
        logger.exception("content ingest 失败")
        return JSONResponse({"error": str(e)}, status_code=400)


async def api_content_seed_demo(request: Request) -> Response:
    """POST: {club_id} → 灌入演示语料(历史层 + 当年层, design/11 §10)。"""
    try:
        body = await request.json()
        return JSONResponse(_content_rt().seed_demo(
            body.get("club_id", "demo_club"), session_id=body.get("session_id", "demo")))
    except Exception as e:
        logger.exception("content seed 失败")
        return JSONResponse({"error": str(e)}, status_code=400)


async def api_content_state(request: Request) -> Response:
    """GET /api/content/state?club_id=x → 素材图 + 权威度徽章 + 时间线 + 工作流定义。"""
    try:
        club = request.query_params.get("club_id", "default")
        return JSONResponse(_content_rt().state(club))
    except Exception as e:
        logger.exception("content state 失败")
        return JSONResponse({"error": str(e)}, status_code=400)


async def api_content_gaps(request: Request) -> Response:
    """GET /api/content/gaps?club_id=x → ② 缺口检测(五类, 零 LLM) + 待核实清单。"""
    try:
        club = request.query_params.get("club_id", "default")
        return JSONResponse(_content_rt().detect_gaps(club))
    except Exception as e:
        logger.exception("content gaps 失败")
        return JSONResponse({"error": str(e)}, status_code=400)


async def api_content_topics(request: Request) -> Response:
    """GET /api/content/topics?club_id=x&scan=1 → 选题库(scan=1 时先做一次扫描入库)。"""
    try:
        club = request.query_params.get("club_id", "default")
        rt = _content_rt()
        if request.query_params.get("scan", "1") not in ("0", "false", "no"):
            return JSONResponse(rt.scan_topics(club))
        ts = rt.topics_for(club)
        return JSONResponse({"club_id": club, "topics": ts.records(),
                             "backend": ts.backend, "count": ts.count()})
    except Exception as e:
        logger.exception("content topics 失败")
        return JSONResponse({"error": str(e)}, status_code=400)


async def api_content_draft(request: Request) -> Response:
    """POST: {club_id, topic?, max_segments?, use_llm?} → ③ 选题成稿(决策链 → 段结构)。"""
    try:
        body = await request.json()
        return JSONResponse(_content_rt().draft(
            body.get("club_id", "default"), topic=body.get("topic"),
            max_segments=int(body.get("max_segments") or 6),
            use_llm=bool(body.get("use_llm")),
            intents=body.get("intents")))
    except Exception as e:
        logger.exception("content draft 失败")
        return JSONResponse({"error": str(e)}, status_code=400)


async def api_content_verify(request: Request) -> Response:
    """POST: {club_id, topic?, segments?} → ④ 事实核验(断言溯源, 阻断无据) + 门禁判定。

    segments 可选：工作台里稿件是**用户拖拽组装**出来的，所以核验对象就是前端组装结果
    （同一套断言溯源规则，零 LLM）；不传时按已保存的最新稿件版本核验。
    """
    try:
        body = await request.json()
        club = body.get("club_id", "default")
        segs = body.get("segments")
        if isinstance(segs, list) and segs:
            # composed=True：这是用户拖拽组装的稿，落"组装轨"，不覆盖系统成稿的轨一基线
            draft = {"topic": body.get("topic") or "", "segments": segs, "composed": True}
            return JSONResponse(_content_rt().verify(club, draft=draft))
        return JSONResponse(_content_rt().verify(club))
    except Exception as e:
        logger.exception("content verify 失败")
        return JSONResponse({"error": str(e)}, status_code=400)


async def api_content_platforms(request: Request) -> Response:
    """POST: {club_id, force?} → ⑤ 多平台适配(有阻断项默认不放行)。"""
    try:
        body = await request.json()
        return JSONResponse(_content_rt().platform_versions(
            body.get("club_id", "default"), force=bool(body.get("force"))))
    except Exception as e:
        logger.exception("content platforms 失败")
        return JSONResponse({"error": str(e)}, status_code=400)


async def api_content_preview(request: Request) -> Response:
    """POST: {segments} → 三渠道实时预览(零 LLM, 不过核验门禁)。

    和 /api/content/platforms 的分工: 预览是**组装过程中看样貌**(还没定稿就该能看见),
    发布才是过门禁的适配。预览不写库、不留痕, 拖动时随手调。
    """
    try:
        body = await request.json()
        from content.preview import preview_channels
        return JSONResponse(preview_channels(body.get("segments") or []))
    except Exception as e:
        logger.exception("content preview 失败")
        return JSONResponse({"error": str(e)}, status_code=400)


async def api_content_assist(request: Request) -> Response:
    """POST: {club_id, segments, roles?} → 六角色智能辅助(确定性优先)。

    勾几个角色就跑几个; 结果里除文字意见外, 每条可应用的改动带一个 action
    (drop_blocks / mask_spans / split_blocks / trim_to), 由用户在界面上决定应不应用。
    没有 API Key 也照样出结果 —— 六个角色的判断全部基于确定性核验与渲染。
    """
    try:
        body = await request.json()
        club = body.get("club_id", "default")
        roles = body.get("roles") or None
        segs = body.get("segments") or []
        if not isinstance(segs, list):
            return JSONResponse({"error": "segments 必须是数组"}, status_code=400)
        # 素材库用于断言级核验与来源档位统计(不传就只用稿件自身信息)
        try:
            mats = _content_rt().state(club).get("materials") or []
        except Exception:  # noqa: BLE001
            mats = []
        from content.assist import run_assist
        return JSONResponse(run_assist(segs, roles, mats, club_id=club))
    except Exception as e:
        logger.exception("content assist 失败")
        return JSONResponse({"error": str(e)}, status_code=400)


async def api_content_assist_roles(request: Request) -> Response:
    """GET /api/content/assist_roles → 六个角色的 id/名称/职责(前端角色条由它驱动)。"""
    from content.assist import ROLES
    return JSONResponse({"roles": ROLES})


async def api_content_rollback(request: Request) -> Response:
    """POST: {club_id, to_seq, reason?} → 素材图回滚 + 稿件联动回退(验收 A2)。"""
    try:
        body = await request.json()
        return JSONResponse(_content_rt().rollback(
            body.get("club_id", "default"), int(body.get("to_seq") or 0),
            reason=body.get("reason", "")))
    except Exception as e:
        logger.exception("content rollback 失败")
        return JSONResponse({"error": str(e)}, status_code=400)


async def api_content_versions(request: Request) -> Response:
    """GET /api/content/versions?club_id=x&seq=N → 稿件版本列表 / 指定 seq 的版本。"""
    try:
        club = request.query_params.get("club_id", "default")
        seq = request.query_params.get("seq")
        rt = _content_rt()
        if seq is not None:
            return JSONResponse(rt.draft_at(club, int(seq)))
        from content.content_versions import versions
        # current = 轨一基线（平台渲染/回滚认它）；compose = 用户拖拽组装的那一版
        # （前端首屏用它恢复稿件板，所以刷新后看到的是自己改的稿）
        return JSONResponse({"club_id": club, "versions": versions(club),
                             "current": rt.current_draft(club),
                             "compose": rt.compose_draft(club)})
    except Exception as e:
        logger.exception("content versions 失败")
        return JSONResponse({"error": str(e)}, status_code=400)


async def api_content_checklist(request: Request) -> Response:
    """GET /api/content/checklist?club_id=x → 《待核实清单》(可核查报告的证据)。"""
    try:
        club = request.query_params.get("club_id", "default")
        g = _content_rt().detect_gaps(club)
        return JSONResponse({"club_id": club, "check_list": g["check_list"],
                             "summary": g["summary"]})
    except Exception as e:
        logger.exception("content checklist 失败")
        return JSONResponse({"error": str(e)}, status_code=400)


async def api_content_review_record(request: Request) -> Response:
    """GET /api/content/review_record?club_id=x → ⑥ 审核留痕(导出用结构化记录)。"""
    try:
        club = request.query_params.get("club_id", "default")
        return JSONResponse(_content_rt().review_record(club))
    except Exception as e:
        logger.exception("content review_record 失败")
        return JSONResponse({"error": str(e)}, status_code=400)


async def api_content_review_export(request: Request) -> Response:
    """GET /api/content/review_export?club_id=x → 下载 Markdown 版《审核记录》。"""
    try:
        club = request.query_params.get("club_id", "default")
        rec = _content_rt().review_record(club)["record"]
        md = _review_record_markdown(rec)
        return Response(md, media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition":
                                 f'attachment; filename="review_record_{club}.md"'})
    except Exception as e:
        logger.exception("content review_export 失败")
        return JSONResponse({"error": str(e)}, status_code=400)


async def api_content_export_package(request: Request) -> Response:
    """GET /api/content/export_package?club_id=x → 下载"宣发包"(三平台物料)。"""
    try:
        club = request.query_params.get("club_id", "default")
        rt = _content_rt()
        pv = rt.platform_versions(club, force=True)
        from content.render import export_package
        md = export_package("", pv.get("platforms") or {},
                            rt.current_draft(club).get("topic") or "")
        return Response(md, media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition":
                                 f'attachment; filename="package_{club}.md"'})
    except Exception as e:
        logger.exception("content export_package 失败")
        return JSONResponse({"error": str(e)}, status_code=400)


async def api_content_dashboard(request: Request) -> Response:
    """GET /api/content/dashboard?club_id=x → 仪表盘(确定性操作 vs LLM 调用)。"""
    try:
        club = request.query_params.get("club_id", "default")
        return JSONResponse(_content_rt().dashboard(club))
    except Exception as e:
        logger.exception("content dashboard 失败")
        return JSONResponse({"error": str(e)}, status_code=400)


async def api_content_resolve_conflict(request: Request) -> Response:
    """POST: {club_id, seg_id?, chosen_eid} → 用户点选矛盾的官方口径(人在环里)。

    记录用户裁决(写进报告行)并把选定版本作为该段的依据; 真实改写仍走确定性渲染,
    不用 LLM —— 用户选了哪一版, 系统就照哪一版写。
    """
    try:
        body = await request.json()
        club = body.get("club_id", "default")
        chosen_eid = body.get("chosen_eid") or ""
        rt = _content_rt()
        st = rt.store_for(club)
        ev = st.latest().get("events", {}).get(chosen_eid)
        if ev is None:
            return JSONResponse({"error": f"素材不存在: {chosen_eid}"}, status_code=400)
        rt._append_step(club, "fact_verify", action="user_resolve_conflict",
                        chosen_eid=chosen_eid, seg_id=body.get("seg_id") or "",
                        note=body.get("note", ""))
        # 闭环: 把该矛盾标为"已由用户选定此版本", 再按同一决策链重出一稿并重新核验。
        # 重出仍然零 LLM —— 用户选了哪一版, 系统就照哪一版写(系统不替用户选, 只照办)。
        redraft = None
        try:
            redraft = rt.re_draft_with_resolution(club, chosen_eid,
                                                  seg_id=body.get("seg_id") or "")
        except Exception as e:  # noqa: BLE001
            logger.warning("裁决后重出稿件失败(裁决已记录): %s", e)
        return JSONResponse({"club_id": club, "resolved": True,
                             "chosen_eid": chosen_eid,
                             "chosen_text": (ev.get("content") or "")[:200],
                             "redraft": redraft,
                             "note": "用户裁决已记录, 稿件按选定口径重出"})
    except Exception as e:
        logger.exception("content resolve_conflict 失败")
        return JSONResponse({"error": str(e)}, status_code=400)


# ---------- 内容域 LLM 授权点(design/11 §6: 每点都过闸门, 未放行就降级) ----------
def _content_llm():
    from content import content_llm
    return content_llm


async def api_content_select_topic(request: Request) -> Response:
    """POST: {club_id} → P8 素材库 RAG 出选题(零 LLM 粗筛 top8 → LLM 挑 1 条)。

    闸门未放行/无 Key 时返回按分数排序的候选池, 不报错(前端照常可用)。
    """
    try:
        body = await request.json()
        club = body.get("club_id", "default")
        _apply_user_llm(request, club)
        rt = _content_rt()
        cands = rt.scan_topics(club, persist=False)["topics"]
        return JSONResponse(await _content_llm().select_topic(
            cands, rt.materials(club), club_id=club, runtime=None))
    except Exception as e:
        logger.exception("content select_topic 失败")
        return JSONResponse({"error": str(e)}, status_code=400)


async def api_content_polish(request: Request) -> Response:
    """POST: {club_id, tone?, max_chars?} → P24 授权润色(产物必须过断言级 diff)。

    未授权/无 Key → 返回轨一事实基线并把原因写在 `llm.error` 里(不报错)。
    """
    try:
        body = await request.json()
        club = body.get("club_id", "default")
        _apply_user_llm(request, club)
        rt = _content_rt()
        cur = rt.current_draft(club)
        from content.content_versions import draft_to_text
        base = cur.get("text") or draft_to_text(cur)
        out = await _content_llm().polish(
            cur, base, club_id=club, runtime=None,
            tone=body.get("tone", ""),
            max_chars=int(body["max_chars"]) if body.get("max_chars") else None)
        if out.get("polished"):
            from content.content_versions import save_version
            save_version(club, rt.store_for(club).head_seq,
                         {**cur, "topic": cur.get("topic")}, track="polished",
                         text=out["text"], llm_used=True)
        return JSONResponse(out)
    except Exception as e:
        logger.exception("content polish 失败")
        return JSONResponse({"error": str(e)}, status_code=400)


async def api_content_compliance(request: Request) -> Response:
    """POST: {club_id} → P26 授权合规与风险复核(只提示风险, 不下结论)。"""
    try:
        body = await request.json()
        club = body.get("club_id", "default")
        _apply_user_llm(request, club)
        rt = _content_rt()
        cur = rt.current_draft(club)
        from content.content_versions import draft_to_text
        text = cur.get("text") or draft_to_text(cur)
        # 确定性部分先给: 隐私模式检测(零 LLM), LLM 只做补充意见
        from content.fact_verifier import detect_sensitive
        det = detect_sensitive(text)
        out = await _content_llm().compliance_check(text, club_id=club,
                                                   runtime=None)
        out["deterministic"] = [{"kind": h["pattern"], "quote": h["sample"]}
                                for h in det]
        return JSONResponse(out)
    except Exception as e:
        logger.exception("content compliance 失败")
        return JSONResponse({"error": str(e)}, status_code=400)


async def api_content_visual(request: Request) -> Response:
    """POST: {club_id} → P28 授权配图文案与短视频分镜。"""
    try:
        body = await request.json()
        club = body.get("club_id", "default")
        _apply_user_llm(request, club)
        rt = _content_rt()
        cur = rt.current_draft(club)
        from content.content_versions import draft_to_text
        text = cur.get("text") or draft_to_text(cur)
        return JSONResponse(await _content_llm().pictogram(text, club_id=club,
                                                          runtime=None))
    except Exception as e:
        logger.exception("content visual 失败")
        return JSONResponse({"error": str(e)}, status_code=400)


async def api_content_adapt_platform(request: Request) -> Response:
    """POST: {club_id, platform} → P21 跨平台改写(默认点; 产物同样过断言级 diff)。"""
    try:
        body = await request.json()
        club = body.get("club_id", "default")
        _apply_user_llm(request, club)
        rt = _content_rt()
        cur = rt.current_draft(club)
        from content.content_versions import draft_to_text
        base = cur.get("text") or draft_to_text(cur)
        return JSONResponse(await _content_llm().adapt_platform(
            base, body.get("platform", "wechat"), club_id=club,
            runtime=None))
    except Exception as e:
        logger.exception("content adapt_platform 失败")
        return JSONResponse({"error": str(e)}, status_code=400)


def _review_record_markdown(rec: dict) -> str:
    """审核记录 → Markdown(给审稿人看的证据与记录, 不是审核结论)。"""
    lines = [f"# 审核记录 · {rec.get('club_id')}",
             f"\n生成时间: {rec.get('generated_at')} · 素材图 seq: {rec.get('head_seq')}",
             f"\n> {rec.get('disclaimer')}"]
    lines.append("\n## 一、素材清单(带来源权威度)")
    lines.append("| 素材 | 类型 | 来源 | 权威度 | 已确认 | 含隐私 |")
    lines.append("|---|---|---|---|---|---|")
    for m in rec.get("materials") or []:
        lines.append(f"| {m.get('subject')} | {m.get('type')} | {m.get('source')} | "
                     f"{m.get('authority')} | {'是' if m.get('verified') else '否'} | "
                     f"{'是' if m.get('sensitive') else '否'} |")
    lines.append("\n## 二、缺口与处置")
    lines.append("| 类型 | 断言 | 素材 | 阻断 | 说明 |")
    lines.append("|---|---|---|---|---|")
    for g in rec.get("gaps") or []:
        lines.append(f"| {g.get('label')} | {g.get('assertion')} | {g.get('source_eid')} | "
                     f"{'是' if g.get('blocking') else '否'} | {g.get('reason')} |")
    lines.append("\n## 三、稿件")
    d = rec.get("draft") or {}
    lines.append(f"主题: {d.get('topic')} · 阻断: {'是' if d.get('block') else '否'}")
    for seg in d.get("segments") or []:
        refs = "、".join(seg.get("source_refs") or []) or "—"
        lines.append(f"- **{seg.get('label') or seg.get('kind')}**[{seg.get('status')}] "
                     f"{seg.get('text')}   ← 来源: {refs}")
    lines.append("\n## 四、稿件版本")
    for v in rec.get("versions") or []:
        lines.append(f"- seq={v.get('seq')} {v.get('track_label')} "
                     f"{v.get('chars')}字 阻断={v.get('block')}")
    llm = rec.get("llm") or {}
    lines.append("\n## 五、LLM 用量与人机分工")
    lines.append(f"- 默认点调用: {llm.get('default', 0)} 次; "
                 f"授权点调用: {llm.get('authorized', 0)} 次")
    lines.append(f"- 分点明细: {llm.get('by_point') or {}}")
    lines.append(f"- 素材图回滚次数: {rec.get('rollbacks', 0)}")
    return "\n".join(lines)


# ---------- 复盘智能体(design/09 + v0.4 一体化: 与陪练共享数据/快照/状态) ----------
def _review_rt():
    return get_review_runtime()


async def api_review_snapshot(request: Request) -> Response:
    """GET /api/review/snapshot?student_id=x&node_id=qa_N → 节点当时状态物化(只读)。

    用素材图回滚技术(state_at 快照+重放)重建该节点时刻的 素材库/稿件,
    不动在线数据。进入节点时前端调用。
    """
    from components import review_bridge
    sid = request.query_params.get("student_id", "default")
    try:
        snap = review_bridge.snapshot(sid, request.query_params.get("node_id", "root"))
        return JSONResponse(snap, status_code=200 if "error" not in snap else 400)
    except Exception as e:
        logger.exception("review snapshot 失败")
        return JSONResponse({"error": str(e)}, status_code=400)


async def api_review_experts(request: Request) -> Response:
    """GET /api/review/experts → 6 专家目录(UI 卡片)。"""
    return JSONResponse({"experts": _review_rt().experts()})


async def api_review_graph(request: Request) -> Response:
    """GET /api/review/graph?club_id=x → 复盘节点图(根=素材库 + 每次成稿的时间轴)。"""
    sid = request.query_params.get("student_id", "default")
    try:
        return JSONResponse(_review_rt().graph(sid))
    except Exception as e:
        logger.exception("review graph 失败")
        return JSONResponse({"error": str(e)}, status_code=400)


async def api_review_create(request: Request) -> Response:
    """POST: {student_id, node_id?, round_id?, roles?} → 新建复盘(常驻团队, 返回 review_id)。

    roles: 只让其中几个角色发言(界面上六个角色可多选); 省略 = 全部。
    """
    try:
        body = await request.json()
        sid = body.get("student_id", "default")
        # 复盘团队 LLM 用该工作区自己的配置(用户配置优先, 回退 .env)
        agent_cfg = _resolve_agent_config(sid)
        result = await _review_rt().create(
            sid, body.get("node_id", "root"),
            round_id=body.get("round_id"), agent_cfg=agent_cfg,
            roles=body.get("roles") or None)
        return JSONResponse(result)
    except Exception as e:
        logger.exception("review create 失败")
        return JSONResponse({"error": str(e)}, status_code=400)


async def api_review_resume(request: Request) -> Response:
    """POST: {review_id} → 打开已有复盘（进程内重连 / 重启后 COLD_RECOVER）。"""
    try:
        body = await request.json()
        rid = body.get("review_id", "")
        # 用会话落盘的学生 ID 解析同一份 LLM 配置
        from components.review_runtime import ReviewRuntime
        ses = _review_rt().get(rid)
        sid = ses.student_id if ses is not None else None
        if sid is None:
            rec = ReviewRuntime._load_session(_review_rt(), rid)
            sid = (rec or {}).get("student_id", "default")
        agent_cfg = _resolve_agent_config(sid or "default")
        result = await _review_rt().resume(rid, agent_cfg=agent_cfg)
        return JSONResponse(result)
    except Exception as e:
        logger.exception("review resume 失败")
        return JSONResponse({"error": str(e)}, status_code=400)


async def api_review_interact(request: Request) -> Response:
    """POST: {review_id, body, target?} → 把下一条消息喂给仍在运行的成员。"""
    try:
        body = await request.json()
        result = await _review_rt().interact(
            body.get("review_id", ""), body.get("body", ""),
            target=body.get("target"))
        return JSONResponse(result)
    except Exception as e:
        logger.exception("review interact 失败")
        return JSONResponse({"error": str(e)}, status_code=400)


async def api_review_status(request: Request) -> Response:
    """GET /api/review/status?review_id=x → 复盘会话状态。"""
    return JSONResponse(_review_rt().status(request.query_params.get("review_id", "")))


async def api_review_hots(request: Request) -> Response:
    """POST: {review_id, command, payload} → 总编模式命令(re_verify / re_draft)。

    payload 可空: 稿件、待核实清单、素材库摘要由系统从该复盘节点绑定的事实里自动装填
    (design/11 §9 T3.3: 一次指令 → 多平台版本并行产出)。旧命令名 re_score/re_question 仍兼容。
    """
    try:
        body = await request.json()
        rid = body.get("review_id", "")
        ses = _review_rt().get(rid)
        sid = ses.student_id if ses is not None else "default"
        # 与团队/工作台共用同一份 LLM 配置
        agent_cfg = _resolve_agent_config(sid)
        result = await _review_rt().hots(
            rid, body.get("command", ""), body.get("payload") or {},
            agent_cfg=agent_cfg)
        return JSONResponse(result)
    except Exception as e:
        logger.exception("review hots 失败")
        return JSONResponse({"error": str(e)}, status_code=400)


async def api_content_review(request: Request) -> Response:
    """POST: {club_id, seq?, roles?} → 基于最近一次成稿开一场**采编复盘**(6 角色)。

    seq 省略时用当前稿件版本; 复盘绑定该 seq 的素材库快照(design/11 §9 T3.2)。
    roles 是界面上勾选的几个角色(可多选); 省略 = 六个全上。

    这条是**走 API 的多智能体路线**(SwarmFlow/团队), 和 /api/content/assist 的
    确定性六角色是两套东西: 后者零 Key 可跑、结论可复现; 前者会真的调大模型。
    """
    try:
        body = await request.json()
        club = body.get("club_id", "default")
        _apply_user_llm(request, club)
        roles = body.get("roles") or None
        if roles is not None and not isinstance(roles, list):
            return JSONResponse({"error": "roles 必须是数组"}, status_code=400)
        rt = _content_rt()
        cur = rt.current_draft(club)
        seq = body.get("seq")
        seq = int(seq) if seq is not None else int(cur.get("seq") or rt.store_for(club).head_seq)
        node_id = f"draft:{seq}"
        from components import review_graph
        if review_graph.get_node(club, node_id) is None:
            node_id = "root"
        agent_cfg = _resolve_agent_config(club)
        result = await _review_rt().create(club, node_id, agent_cfg=agent_cfg,
                                          roles=roles)
        result["club_id"] = club
        result["node_id"] = node_id
        result["draft_seq"] = seq
        result["roles"] = roles or None
        return JSONResponse(result)
    except Exception as e:
        logger.exception("content review 创建失败")
        return JSONResponse({"error": str(e)}, status_code=400)


async def api_review_close(request: Request) -> Response:
    """POST: {review_id, discard?} → 结束复盘; discard=true 同时删除复盘落盘(切换菜单丢弃新建)。"""
    try:
        body = await request.json()
        result = await _review_rt().close(body.get("review_id", ""),
                                          discard=bool(body.get("discard", False)))
        return JSONResponse(result)
    except Exception as e:
        logger.exception("review close 失败")
        return JSONResponse({"error": str(e)}, status_code=400)


async def api_review_list(request: Request) -> Response:
    """GET /api/review/list?student_id=x → 进行中的复盘列表。"""
    sid = request.query_params.get("student_id") or None
    return JSONResponse(_review_rt().list_reviews(sid))


async def api_review_stream(request: Request) -> Response:
    """GET /api/review/stream?review_id=x → SSE：团队的流式输出。"""
    rid = request.query_params.get("review_id", "")

    async def gen():
        async for line in _review_rt().iter_stream(rid):
            yield f"data: {line}\n\n"

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def review_index(request: Request) -> Response:
    """旧的独立复盘页已退休: 采编复盘并入工作台(design/11 §9 T3.4), 这里回工作台。

    `/api/review/*` 全部保留(六角色阵容、SSE 流、总编模式都在), 只是不再有独立页面。
    """
    from starlette.responses import RedirectResponse
    return RedirectResponse("/workbench")


class _AuthMiddleware:
    """进程令牌鉴权中间件(Starlette 1.x 类式中间件)。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        if _auth_ok(request):
            await self.app(scope, receive, send)
            return
        response = JSONResponse({"error": "unauthorized"}, status_code=401)
        await response(scope, receive, send)


# 启动时加载用户配置
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: Starlette):
    # 启动时执行
    await load_user_configs()
    logger.info("用户配置已加载")
    try:
        from components.mongo_store import init_indexes, mongo_available  # 阶段2 T2.0 自检
        if mongo_available():
            init_indexes()
            logger.info("MongoDB 题目库/意图库索引自检完成")
        else:
            logger.warning("MongoDB 不可用: 选题库功能降级(选题扫描回退文件兜底)")
    except Exception as e:  # noqa: BLE001
        logger.warning("MongoDB 初始化跳过: %s", e)
    yield
    # 关闭时执行（如果需要）
    # 清理资源等

def create_app() -> Starlette:
    routes = [
        Route("/", index),
        Route("/workbench", workbench),
        Route("/api/health", api_health),
        Route("/api/config", api_config),
        Route("/api/auth/register", api_auth_register, methods=["POST"]),
        Route("/api/auth/login", api_auth_login, methods=["POST"]),
        Route("/api/auth/logout", api_auth_logout, methods=["POST"]),
        Route("/api/auth/me", api_auth_me),
        Route("/api/user/llm", api_user_llm_get),
        Route("/api/user/llm", api_user_llm_set, methods=["POST"]),
        Route("/api/user/llm/test", api_user_llm_test, methods=["POST"]),
        # ---- 内容域(六步两相, design/11 §5.5) ----
        Route("/api/content/analyze", api_content_analyze, methods=["POST"]),
        Route("/api/content/ingest", api_content_ingest, methods=["POST"]),
        Route("/api/content/seed_demo", api_content_seed_demo, methods=["POST"]),
        Route("/api/content/state", api_content_state),
        Route("/api/content/gaps", api_content_gaps),
        Route("/api/content/checklist", api_content_checklist),
        Route("/api/content/topics", api_content_topics),
        Route("/api/content/draft", api_content_draft, methods=["POST"]),
        Route("/api/content/verify", api_content_verify, methods=["POST"]),
        Route("/api/content/platforms", api_content_platforms, methods=["POST"]),
        Route("/api/content/preview", api_content_preview, methods=["POST"]),
        Route("/api/content/assist", api_content_assist, methods=["POST"]),
        Route("/api/content/assist_roles", api_content_assist_roles),
        Route("/api/content/rollback", api_content_rollback, methods=["POST"]),
        Route("/api/content/versions", api_content_versions),
        Route("/api/content/resolve_conflict", api_content_resolve_conflict,
              methods=["POST"]),
        Route("/api/content/dashboard", api_content_dashboard),
        Route("/api/content/review_record", api_content_review_record),
        Route("/api/content/review_export", api_content_review_export),
        Route("/api/content/export_package", api_content_export_package),
        Route("/api/content/review", api_content_review, methods=["POST"]),
        # ---- LLM 调用点(P8/P21/P24/P26/P28; 未授权/无 Key 一律降级, 不报错) ----
        Route("/api/content/select_topic", api_content_select_topic, methods=["POST"]),
        Route("/api/content/polish", api_content_polish, methods=["POST"]),
        Route("/api/content/compliance", api_content_compliance, methods=["POST"]),
        Route("/api/content/visual", api_content_visual, methods=["POST"]),
        Route("/api/content/adapt_platform", api_content_adapt_platform,
              methods=["POST"]),
        Route("/review", review_index),
        Route("/api/review/graph", api_review_graph),
        Route("/api/review/snapshot", api_review_snapshot),
        Route("/api/review/experts", api_review_experts),
        Route("/api/review/create", api_review_create, methods=["POST"]),
        Route("/api/review/resume", api_review_resume, methods=["POST"]),
        Route("/api/review/interact", api_review_interact, methods=["POST"]),
        Route("/api/review/status", api_review_status),
        Route("/api/review/hots", api_review_hots, methods=["POST"]),
        Route("/api/review/close", api_review_close, methods=["POST"]),
        Route("/api/review/list", api_review_list),
        Route("/api/review/stream", api_review_stream),
        Mount("/static", app=StaticFiles(directory=_STATIC_DIR)),
    ]
    _cors_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
    _allowed_hosts = [h.strip() for h in
                      os.getenv("WEB_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]
    _allowed_hosts = list(dict.fromkeys(_allowed_hosts + ["testserver"]))  # testserver 供 TestClient
    middleware = [
        # 防 DNS rebinding: 只接受本机 Host
        Middleware(TrustedHostMiddleware, allowed_hosts=_allowed_hosts),
        # CORS 默认关闭(同源不需要); 如需远程前端, 用 CORS_ORIGINS 显式列白名单
        Middleware(CORSMiddleware, allow_origins=_cors_origins,
                   allow_methods=["*"], allow_headers=["*"]),
        # 进程令牌鉴权(/api/*, health 豁免; AUTH_ENABLED=0 关闭)
        Middleware(_AuthMiddleware),
    ]
    return Starlette(routes=routes, middleware=middleware, lifespan=lifespan)

app = create_app()


if __name__ == "__main__":
    host = os.getenv("WEB_HOST", "127.0.0.1")
    port = int(os.getenv("WEB_PORT", "8080"))
    print("=" * 64)
    print("新媒体信宣智能体 · 三栏工作台")
    print(f"界面: http://{host}:{port}/workbench   (根路径 / 也指向它)")
    print("六步两相: 素材录入 → 缺口检测 → 选题成稿 → 事实核验 → 多平台适配 → 审核留痕")
    print("核心主张: 稿件每句话都能点回原素材(溯源链); 零 LLM 也能跑通全流程")
    print("=" * 64)
    uvicorn.run(app, host=host, port=port, log_level="info")