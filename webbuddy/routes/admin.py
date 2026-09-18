"""管理面板 API：登录、账号管理、API Key、设置、用量统计。"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import threading
import time
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

from converter import auth_dirs

from .. import config, state
from ..auth import (
    SESSION_COOKIE_NAME,
    AuthMiddleware,
    clear_login_failures,
    clear_sessions,
    create_session,
    drop_session,
    get_admin_settings,
    get_admin_username,
    get_password_hash,
    hash_password,
    login_allowed,
    login_client_id,
    record_login_failure,
    save_admin_settings,
    session_username,
    verify_password,
)

router = APIRouter()

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def _static_page(name: str) -> str:
    return (_STATIC_DIR / name).read_text(encoding="utf-8")


@router.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)


@router.get("/login", response_class=HTMLResponse)
def login_page():
    return _static_page("login.html")


@router.post("/api/login")
async def api_login(request: Request, body: dict):
    client_id = login_client_id(request)
    allowed, retry_after = login_allowed(client_id)
    if not allowed:
        return JSONResponse(
            {"error": "登录尝试过于频繁，请稍后再试", "retry_after": retry_after},
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )

    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))
    settings = get_admin_settings()
    username_ok = hmac.compare_digest(
        username.casefold(), str(settings.get("admin_username", "")).casefold()
    )
    password_ok = verify_password(password, str(settings.get("password_hash", "")))
    if not (username_ok and password_ok):
        record_login_failure(client_id)
        return JSONResponse({"error": "用户名或密码错误"}, status_code=401)

    clear_login_failures(client_id)
    # 登录成功时自动升级旧版无盐 SHA-256 哈希。
    if not str(settings.get("password_hash", "")).startswith("pbkdf2_sha256$"):
        settings["password_hash"] = hash_password(password)
        save_admin_settings(settings)

    token = create_session(str(settings["admin_username"]))
    response = JSONResponse({"success": True, "username": settings["admin_username"]})
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=int(os.environ.get("WEBBUDDY_SESSION_EXPIRY", "86400")),
        httponly=True,
        secure=config.CONFIG.get("secure_cookie", request.url.scheme == "https"),
        samesite="lax",
        path="/",
    )
    return response


@router.post("/api/logout")
async def api_logout(request: Request):
    drop_session(request.cookies.get(SESSION_COOKIE_NAME))
    response = JSONResponse({"success": True})
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response


@router.get("/api/check-auth")
async def check_auth(request: Request):
    username = session_username(request.cookies.get(SESSION_COOKIE_NAME))
    if username:
        return {"authenticated": True, "username": username}
    return JSONResponse({"authenticated": False}, status_code=401)


@router.post("/api/change-password")
async def change_password(request: Request, body: dict):
    current_password = str(body.get("current_password") or body.get("old_password") or "")
    new_password = str(body.get("new_password") or "")
    new_username = str(body.get("username") or get_admin_username()).strip()
    if len(new_username) < 3 or len(new_username) > 254 or any(ord(c) < 32 for c in new_username):
        return JSONResponse({"error": "用户名格式无效"}, status_code=400)
    if new_password and len(new_password) < 10:
        return JSONResponse({"error": "新密码至少10位"}, status_code=400)
    if not verify_password(current_password, get_password_hash()):
        return JSONResponse({"error": "当前密码错误"}, status_code=403)
    settings = get_admin_settings()
    settings["admin_username"] = new_username
    if new_password:
        settings["password_hash"] = hash_password(new_password)
    save_admin_settings(settings)
    clear_sessions()
    response = JSONResponse({"success": True, "message": "管理员凭据已更新，请重新登录"})
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response


@router.get("/", response_class=HTMLResponse)
def index():
    return _static_page("index.html")


@router.get("/api/auth-files/local")
async def list_local_auth_files():
    """扫描本地 WorkBuddy auth 目录，返回所有可用的 auth 文件。"""
    files = []
    for auth_dir in auth_dirs():
        if not auth_dir.is_dir():
            continue
        for f in sorted(auth_dir.glob("*.info")):
            try:
                data = json.loads(f.read_text("utf-8"))
                acct = data.get("account") or {}
                nickname = acct.get("nickname", f.stem)
                files.append({
                    "path": str(f),
                    "filename": f.name,
                    "nickname": nickname,
                    "already_imported": nickname in state.account_store.list_nicknames(),
                })
            except Exception:
                files.append({
                    "path": str(f),
                    "filename": f.name,
                    "nickname": f.stem,
                    "already_imported": f.stem in state.account_store.list_nicknames(),
                })
    return {"files": files}


@router.get("/api/accounts")
def list_accounts():
    rows = state.account_store.list()
    for row in rows:
        remote = state.account_automation.get_state(row["nickname"])
        row["sync_status"] = remote.get("sync_status", "pending")
        row["sync_error"] = remote.get("sync_error")
        row["synced_at"] = remote.get("synced_at", 0)
        row["checkin"] = remote.get("checkin")
        row["quota"] = remote.get("quota")
        quota = row["quota"]
        if quota:
            row["remaining"] = quota.get("remaining")
            if quota.get("unlimited"):
                row["status"] = "active"
            elif quota.get("remaining") is not None:
                row["status"] = "active" if quota["remaining"] > 0 else "exhausted"
    return rows


@router.post("/api/accounts/sync")
async def sync_accounts():
    results = await asyncio.to_thread(state.account_automation.sync_all, True)
    return {"success": True, "results": results}


@router.post("/api/accounts/{nickname}/sync")
async def sync_account(nickname: str):
    if state.account_store.get_cm(nickname) is None:
        raise HTTPException(404, "账号不存在")
    result = await asyncio.to_thread(state.account_automation.sync_account, nickname, True)
    return {"success": result.get("sync_status") in {"ok", "partial"}, **result}


@router.post("/api/accounts/{nickname}/checkin")
async def checkin_account(nickname: str):
    """手动签到单个账号，并同时刷新其服务端额度。"""
    if state.account_store.get_cm(nickname) is None:
        raise HTTPException(404, "账号不存在")

    before = state.account_automation.get_state(nickname).get("checkin") or {}
    result = await asyncio.to_thread(
        state.account_automation.sync_account, nickname, True
    )
    checkin = result.get("checkin") or {}
    checked_in = bool(checkin.get("today_checked_in"))
    was_checked_in = bool(before.get("today_checked_in"))

    if checked_in:
        message = "今日已签到" if was_checked_in else "签到成功"
    elif not checkin.get("active", True):
        message = "签到活动暂未开放"
    else:
        message = result.get("sync_error") or "服务端未确认签到成功"

    return {
        "success": checked_in,
        "message": message,
        **result,
    }


def _sync_account_in_background(nickname: str):
    threading.Thread(
        target=state.account_automation.sync_account,
        args=(nickname, True),
        name=f"webbuddy-sync-{nickname}",
        daemon=True,
    ).start()


@router.post("/api/auth-files/import")
async def import_local_auth_files(body: dict):
    """从本地 auth 目录导入指定的文件。"""
    paths = body.get("paths", [])
    results = []
    for p in paths:
        try:
            f = Path(p)
            if not f.exists():
                results.append({"path": p, "success": False, "error": "文件不存在"})
                continue
            raw = f.read_bytes()
            data = json.loads(raw)
            acct = data.get("account") or {}
            nickname = acct.get("nickname", f.stem)
            # 重名处理
            used = set(state.account_store.list_nicknames())
            base = nickname
            counter = 1
            while nickname in used:
                nickname = f"{base}_{counter}"
                counter += 1
            # 保存到 auths 目录
            dest = config.AUTHS_DIR / f"{nickname}.json"
            with open(dest, "wb") as df:
                df.write(raw)
            state.account_store.add(nickname, str(dest))
            _sync_account_in_background(nickname)
            results.append({"nickname": nickname, "success": True})
        except Exception as e:
            results.append({"path": p, "success": False, "error": str(e)})
    return {"success": True, "results": results}


@router.get("/api/auths/{nickname}/download")
async def download_auth(nickname: str):
    """下载指定账号的 auth 文件。"""
    info = state.account_store.get_info(nickname)
    if not info:
        return JSONResponse({"error": "账号不存在"}, status_code=404)
    auth_path = info.get("auth_path")
    if not auth_path or not os.path.exists(auth_path):
        return JSONResponse({"error": "auth 文件不存在"}, status_code=404)
    try:
        data = json.loads(open(auth_path, "r", encoding="utf-8").read())
        return JSONResponse(data)
    except Exception:
        return JSONResponse({"error": "读取 auth 文件失败"}, status_code=500)


@router.post("/api/accounts/upload")
async def upload_auth(files: list[UploadFile] = File(...)):
    config.AUTHS_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for f in files:
        try:
            raw = await f.read()
            data = json.loads(raw)
            acct = data.get("account") or {}
            nickname = acct.get("nickname", f.filename.replace(".json", "").replace(".info", ""))
            # 重名自动加后缀
            used = set(state.account_store.list_nicknames())
            base = nickname
            counter = 1
            while nickname in used:
                nickname = f"{base}_{counter}"
                counter += 1
            # 保存文件
            dest = config.AUTHS_DIR / f"{nickname}.json"
            with open(dest, "wb") as df:
                df.write(raw)
            # 注册账号
            state.account_store.add(nickname, str(dest))
            _sync_account_in_background(nickname)
            results.append({"nickname": nickname, "success": True})
        except Exception as e:
            results.append({"nickname": f.filename, "success": False, "error": str(e)})
    return {"success": True, "results": results}


@router.delete("/api/accounts/{nickname}")
def delete_account(nickname: str):
    state.account_store.remove(nickname)
    state.account_automation.remove_state(nickname)
    return {"success": True}


@router.post("/api/accounts/{nickname}/credit")
async def set_credit(nickname: str, request: Request):
    body = await request.json()
    amount = body.get("initial_credit")
    if amount is None:
        raise HTTPException(400, "initial_credit is required")
    state.account_store.set_initial_credit(nickname, float(amount))
    return {"success": True}


@router.post("/api/accounts/health-check")
async def health_check_all():
    """兼容旧接口：同步所有账号的签到状态和额度。"""
    threading.Thread(
        target=lambda: state.account_automation.sync_all(True), daemon=True
    ).start()
    return {"success": True, "message": "账号同步已启动"}


@router.get("/api/accounts/check/{nickname}")
async def check_account(nickname: str):
    """兼容旧接口：同步单个账号。"""
    if state.account_store.get_cm(nickname) is None:
        raise HTTPException(404, "账号不存在")
    result = await asyncio.to_thread(state.account_automation.sync_account, nickname, True)
    return result


@router.get("/api/keys")
def list_keys():
    return state.api_key_store.list()


@router.post("/api/keys")
async def create_key(request: Request):
    body = await request.json()
    name = body.get("name", "unnamed")
    account_ids = body.get("account_ids", [])
    strategy = body.get("strategy", "round-robin")
    if not account_ids:
        raise HTTPException(400, "至少选择一个账号")
    result = state.api_key_store.create(name, account_ids, strategy)
    return result


@router.delete("/api/keys/{key}")
def delete_key(key: str):
    state.api_key_store.delete(key)
    return {"success": True}


@router.put("/api/settings")
def update_settings(body: dict):
    if "desensitize" in body:
        config.CONFIG["desensitize"] = bool(body["desensitize"])
    settings = get_admin_settings()
    settings["desensitize"] = config.CONFIG["desensitize"]
    save_admin_settings(settings)
    return {"success": True}


@router.get("/api/settings")
def get_settings():
    return {
        "desensitize": bool(config.CONFIG.get("desensitize")),
        "admin_username": get_admin_username(),
        "secure_cookie": bool(config.CONFIG.get("secure_cookie")),
    }


@router.get("/api/models")
def admin_list_models():
    """管理面板用：当前模型列表（官方动态优先，静态兜底）。"""
    from ..models import available_models, fetch_official_models

    dynamic = fetch_official_models() or []
    ordered = available_models()
    return {"models": ordered, "source": "official" if dynamic else "fallback"}


@router.get("/api/usage-today")
def usage_today():
    """北京时间今日 0 点以来的积分用量：总量、按模型、按 key、按账号。"""
    return state.usage_store.query_today()
