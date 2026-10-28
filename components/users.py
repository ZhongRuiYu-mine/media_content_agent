# coding: utf-8
"""components/users.py — 本地账号(注册/登录/会话), 供右上角「设置」用。

设计取舍(为什么是这个范围):

  - **本地账号, 不是身份系统**。这是一个在本机跑的单进程工作台: 账号用来做到
    "换个人登录, 看到的是自己的 API 配置", 不是为了对外提供鉴权。所以密码用
    PBKDF2-HMAC-SHA256 + 每用户随机盐存哈希, 不存明文, 也不引入第三方依赖。
  - 会话令牌持久化到 `data/users.json`: 服务重启后浏览器不必重新登录(演示友好),
    令牌 30 天过期。
  - 和进程令牌(`X-Auth-Token`)是两层东西, 互不替代:
      进程令牌 = 防跨站脚本调用本机服务的 CSRF 防线(谁在调 API);
      用户令牌 = 谁登录了(我看到的是谁的配置)。
  - 不写入任何人的 `student_id`/`club_id`: 内容域的知识图谱仍按 club_id 组织,
    账号只决定"用谁的 API Key 去调 LLM"。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data")
# 测试用 USERS_FILE 指到临时文件, 免得把开发机上的真实账号写脏
_USERS_PATH = os.getenv("USERS_FILE") or os.path.join(_DATA_DIR, "users.json")

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff]{2,20}$")
_MIN_PASSWORD = 6
SESSION_TTL = 30 * 24 * 3600          # 30 天
_PBKDF2_ROUNDS = 120_000

_lock = threading.RLock()
_state: Dict[str, dict] = {"users": {}, "sessions": {}}
_loaded = False


# ---------------------------------------------------------------- 存储
def _load() -> None:
    global _loaded
    if _loaded:
        return
    with _lock:
        if _loaded:
            return
        try:
            if os.path.exists(_USERS_PATH):
                with open(_USERS_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                _state["users"] = data.get("users") or {}
                _state["sessions"] = data.get("sessions") or {}
            else:
                _state["users"] = {}
                _state["sessions"] = {}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("加载账号文件失败(按空库继续): %s", e)
            _state["users"] = {}
            _state["sessions"] = {}
        _loaded = True
        if _purge_expired():
            _save()


def _save() -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)
    tmp = _USERS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _USERS_PATH)


def _purge_expired() -> bool:
    now = time.time()
    dead = [t for t, s in _state["sessions"].items()
            if now - float(s.get("created_at") or 0) > SESSION_TTL]
    for t in dead:
        _state["sessions"].pop(t, None)
    return bool(dead)


# ---------------------------------------------------------------- 密码
def _hash(password: str, salt: str) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             bytes.fromhex(salt), _PBKDF2_ROUNDS)
    return dk.hex()


def _verify(password: str, salt: str, expected: str) -> bool:
    got = _hash(password, salt)
    return hmac.compare_digest(got, expected or "")


# ---------------------------------------------------------------- 账号
def _valid(username: str, password: str) -> Optional[str]:
    if not _USERNAME_RE.match(username or ""):
        return "账号需 2-20 位，只能用中文、字母、数字、下划线"
    if len(password or "") < _MIN_PASSWORD:
        return f"密码至少 {_MIN_PASSWORD} 位"
    return None


def register(username: str, password: str) -> Tuple[bool, dict]:
    """注册并直接登录。返回 (ok, {token?, username?, error?})。"""
    _load()
    username = (username or "").strip()
    err = _valid(username, password or "")
    if err:
        return False, {"error": err}
    with _lock:
        if username in _state["users"]:
            return False, {"error": "这个账号已经注册过了，直接登录"}
        salt = secrets.token_hex(16)
        _state["users"][username] = {
            "salt": salt, "hash": _hash(password, salt),
            "created_at": time.time(),
        }
        token = _new_session(username)
        _save()
    return True, {"token": token, "username": username}


def login(username: str, password: str) -> Tuple[bool, dict]:
    _load()
    username = (username or "").strip()
    with _lock:
        rec = _state["users"].get(username)
        if not rec or not _verify(password or "", rec.get("salt") or "",
                                 rec.get("hash") or ""):
            # 统一说法: 不告诉调用方"账号不存在"还是"密码错"
            return False, {"error": "账号或密码不对"}
        rec["last_login"] = time.time()
        token = _new_session(username)
        _save()
    return True, {"token": token, "username": username}


def _new_session(username: str) -> str:
    token = secrets.token_urlsafe(24)
    _state["sessions"][token] = {"username": username, "created_at": time.time()}
    return token


def logout(token: str) -> bool:
    _load()
    with _lock:
        gone = _state["sessions"].pop(token or "", None)
        if gone:
            _save()
    return bool(gone)


def session_user(token: str) -> Optional[str]:
    """令牌 → 用户名; 过期或不存在返回 None。"""
    if not token:
        return None
    _load()
    with _lock:
        ses = _state["sessions"].get(token)
        if not ses:
            return None
        if time.time() - float(ses.get("created_at") or 0) > SESSION_TTL:
            _state["sessions"].pop(token, None)
            _save()
            return None
        return ses.get("username")


def user_count() -> int:
    _load()
    return len(_state["users"])


def reset_for_test() -> None:
    """测试用: 清空内存态并删掉账号文件(不影响正在跑的服务进程之外的任何东西)。"""
    global _loaded
    with _lock:
        _state["users"] = {}
        _state["sessions"] = {}
        _loaded = True
        try:
            if os.path.exists(_USERS_PATH):
                os.remove(_USERS_PATH)
        except OSError:
            pass


__all__ = ["register", "login", "logout", "session_user", "user_count",
           "reset_for_test", "SESSION_TTL", "_purge_expired"]
