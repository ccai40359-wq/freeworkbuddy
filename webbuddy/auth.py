"""管理员认证：密码哈希、会话、登录限流、认证中间件。"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sys
import threading
import time

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from . import config

DEFAULT_ADMIN_USERNAME = "admin@local.com"
DEFAULT_ADMIN_PASSWORD = os.environ.get("WEBBUDDY_ADMIN_PASSWORD")
SESSION_COOKIE_NAME = "webbuddy_session"
SESSION_EXPIRY = int(os.environ.get("WEBBUDDY_SESSION_EXPIRY", "86400"))
PASSWORD_ITERATIONS = 310_000
LOGIN_WINDOW = 300
LOGIN_MAX_ATTEMPTS = 8

SESSIONS: dict[str, dict] = {}  # token -> {username, expires_at}
LOGIN_ATTEMPTS: dict[str, list[float]] = {}
_AUTH_LOCK = threading.Lock()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
    )
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        if stored_hash.startswith("pbkdf2_sha256$"):
            _, iterations, salt_hex, digest_hex = stored_hash.split("$", 3)
            candidate = hashlib.pbkdf2_hmac(
                "sha256",
                password.encode("utf-8"),
                bytes.fromhex(salt_hex),
                int(iterations),
            )
            return hmac.compare_digest(candidate.hex(), digest_hex)
        # 兼容旧版无盐 SHA-256，成功登录后会自动升级。
        legacy = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return hmac.compare_digest(legacy, stored_hash)
    except (TypeError, ValueError):
        return False


def create_session(username: str) -> str:
    token = secrets.token_hex(32)
    with _AUTH_LOCK:
        SESSIONS[token] = {
            "username": username,
            "expires_at": time.time() + SESSION_EXPIRY,
        }
    return token


def validate_session(token) -> bool:
    if not token:
        return False
    with _AUTH_LOCK:
        session = SESSIONS.get(token)
        if session is None:
            return False
        if time.time() > session["expires_at"]:
            SESSIONS.pop(token, None)
            return False
        return True


def session_username(token):
    if not validate_session(token):
        return None
    with _AUTH_LOCK:
        session = SESSIONS.get(token or "")
        return session.get("username") if session else None


def clear_sessions():
    with _AUTH_LOCK:
        SESSIONS.clear()


def drop_session(token):
    if token:
        with _AUTH_LOCK:
            SESSIONS.pop(token, None)


def _login_client_id(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def login_allowed(client_id: str):
    now = time.time()
    with _AUTH_LOCK:
        attempts = [t for t in LOGIN_ATTEMPTS.get(client_id, []) if now - t < LOGIN_WINDOW]
        LOGIN_ATTEMPTS[client_id] = attempts
        if len(attempts) < LOGIN_MAX_ATTEMPTS:
            return True, 0
        retry_after = max(1, int(LOGIN_WINDOW - (now - attempts[0])))
        return False, retry_after


def record_login_failure(client_id: str):
    with _AUTH_LOCK:
        LOGIN_ATTEMPTS.setdefault(client_id, []).append(time.time())


def clear_login_failures(client_id: str):
    with _AUTH_LOCK:
        LOGIN_ATTEMPTS.pop(client_id, None)


def login_client_id(request: Request) -> str:
    return _login_client_id(request)


# ── 管理员设置（读写 settings.json）─────────────────────────────────

def _load_settings() -> dict:
    if not config.SETTINGS_FILE.exists():
        return {}
    try:
        import json
        with open(config.SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_settings(settings: dict):
    import json
    config.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = config.SETTINGS_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
    os.replace(tmp, config.SETTINGS_FILE)


def get_admin_settings() -> dict:
    """读取管理员设置；首次运行时创建安全哈希，不保存明文密码。"""
    settings = _load_settings()
    changed = False
    if not settings.get("admin_username"):
        settings["admin_username"] = os.environ.get(
            "WEBBUDDY_ADMIN_USERNAME", DEFAULT_ADMIN_USERNAME
        ).strip()
        changed = True
    if not settings.get("password_hash"):
        initial_password = DEFAULT_ADMIN_PASSWORD
        if not initial_password:
            initial_password = secrets.token_urlsafe(18)
            sys.stderr.write(
                f"\n⚠️  WEBBUDDY_ADMIN_PASSWORD 未设置，已生成一次性密码：{initial_password}\n"
                f"   请尽快登录管理面板修改。\n\n"
            )
        settings["password_hash"] = hash_password(initial_password)
        changed = True
    if changed:
        _save_settings(settings)
    return settings


def save_admin_settings(settings: dict):
    _save_settings(settings)


def get_admin_username() -> str:
    return str(get_admin_settings().get("admin_username", DEFAULT_ADMIN_USERNAME))


def get_password_hash() -> str:
    return str(get_admin_settings().get("password_hash", ""))


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # 公开路径：登录页、认证API、以及 /v1/* 的 OpenAI 兼容 API（有独立鉴权）
        if path in {"/login", "/favicon.ico", "/api/login", "/api/check-auth", "/v1"} or path.startswith("/v1/"):
            return await call_next(request)
        # 检查认证
        token = request.cookies.get(SESSION_COOKIE_NAME)
        if not token or not validate_session(token):
            if path.startswith("/api/"):
                return JSONResponse({"error": "未登录"}, status_code=401)
            return RedirectResponse(url="/login")
        return await call_next(request)
