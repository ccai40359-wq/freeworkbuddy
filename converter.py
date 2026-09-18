#!/usr/bin/env python3
"""
codebuddy2openai — 把 CodeBuddy / WorkBuddy 的订阅暴露成标准 OpenAI 兼容 API。

原理（直连后端，原生 function calling）：
  - 读取本机已登录的 CodeBuddy 桌面端凭据（auth 文件里的 token / uid / enterpriseId）。
  - 直接转发到 CodeBuddy 后端 `https://copilot.tencent.com/v2/chat/completions`。
    该后端本身就是标准 OpenAI chat/completions 协议（含原生 tools / tool_calls / SSE 流式）。
  - 转换器只做两件事：①注入鉴权 header（Authorization / X-User-Id 等）
    ②在本地 /v1/* 与后端 /v2/* 之间做路径映射与透传。
  - token 过期时自动调 `/v2/plugin/auth/token/refresh` 刷新，并回写 auth 文件。

跨平台：自动定位 auth 目录（macOS / Windows / Linux）。
依赖：fastapi + uvicorn + httpx（pip install fastapi "uvicorn[standard]" httpx）。

用法：
  python3 converter.py                       # 默认 127.0.0.1:8787
  python3 converter.py --port 9000
  python3 converter.py --api-key mysecret    # 启用客户端鉴权
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
import uvicorn

try:
    from desensitize import desensitize_body
except ImportError:  # 模块缺失时降级为不脱敏
    def desensitize_body(body, roles=("system",), include_tools=True):
        return body

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

BACKEND = "https://copilot.tencent.com"
DEFAULT_DOMAIN = "www.codebuddy.cn"
USER_AGENT = "codebuddy2openai/2.0"

# ---------------------------------------------------------------------------
# 平台相关：定位 auth 目录
# ---------------------------------------------------------------------------

def auth_dirs() -> list[Path]:
    home = Path.home()
    plat = sys.platform
    if plat == "darwin":
        return [home / "Library" / "Application Support" / "CodeBuddyExtension" / "Data" / "Public" / "auth"]
    if plat == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        return [local / "CodeBuddyExtension" / "Data" / "Public" / "auth"]
    xdg = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
    return [xdg / "CodeBuddyExtension" / "Data" / "Public" / "auth"]


def find_auth_files() -> dict[str, Path]:
    """返回 {nickname: Path} 的所有 auth 文件。文件重名时后缀加 _1, _2 去重。"""
    files: dict[str, Path] = {}
    name_counter: dict[str, int] = {}
    for d in auth_dirs():
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.info")):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                acct = data.get("account") or {}
                nickname = acct.get("nickname", f.stem)
            except Exception:
                nickname = f.stem
            if nickname in files:
                name_counter[nickname] = name_counter.get(nickname, 1) + 1
                nickname = f"{nickname}_{name_counter[nickname]}"
            files[nickname] = f
    return files


# ---------------------------------------------------------------------------
# Auth 凭据管理（读 + 自动刷新 + 回写）
# ---------------------------------------------------------------------------

class CredentialManager:
    """从 auth 文件读取凭据；token 临近过期时自动刷新并回写。"""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._cached: dict | None = None
        self._mtime: float = 0.0

    def _read_raw(self) -> dict:
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _load_if_stale(self):
        """若文件 mtime 变了（外部刷新过），重新加载缓存。"""
        try:
            mt = self.path.stat().st_mtime
        except OSError:
            return
        if self._cached is None or mt != self._mtime:
            self._cached = self._read_raw()
            self._mtime = mt

    def _session(self) -> dict:
        self._load_if_stale()
        if self._cached is None:
            raise RuntimeError(f"无法读取 auth 文件：{self.path}")
        return self._cached

    def _is_expired(self) -> bool:
        s = self._session()
        expires_at = (s.get("auth") or {}).get("expiresAt") or 0
        # 提前 60s 判定过期
        return time.time() * 1000 >= (expires_at - 60_000)

    def _refresh(self):
        """调后端刷新 token，写回 auth 文件与缓存。"""
        s = self._session()
        auth = s.get("auth") or {}
        headers = self._build_headers_from(auth, s.get("account") or {})
        headers["X-Refresh-Token"] = auth.get("refreshToken", "")
        headers["X-Auth-Refresh-Source"] = "plugin"
        url = f"{BACKEND}/v2/plugin/auth/token/refresh"
        try:
            with httpx.Client(timeout=15) as c:
                r = c.post(url, headers=headers, json={})
            data = r.json()
        except Exception as e:
            raise RuntimeError(f"刷新 token 网络失败：{e}")
        if data.get("code") != 0 or not data.get("data"):
            raise RuntimeError(f"刷新 token 失败：{data.get('msg', data)}")
        new_auth = data["data"]
        # 继承部分字段
        new_auth["domain"] = new_auth.get("domain") or auth.get("domain")
        new_auth["lastRefreshTime"] = int(time.time() * 1000)
        # 计算 expiresAt（若后端没直接给）
        if not new_auth.get("expiresAt") and new_auth.get("expiresIn"):
            new_auth["expiresAt"] = int(time.time() * 1000) + new_auth["expiresIn"] * 1000
        if not new_auth.get("refreshExpiresAt") and new_auth.get("refreshExpiresIn"):
            new_auth["refreshExpiresAt"] = int(time.time() * 1000) + new_auth["refreshExpiresIn"] * 1000
        s["auth"] = new_auth
        # 原子写回
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        self._cached = s
        self._mtime = self.path.stat().st_mtime

    def _build_headers_from(self, auth: dict, account: dict) -> dict:
        domain = auth.get("domain") or DEFAULT_DOMAIN
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {auth.get('accessToken','')}",
            "X-User-Id": account.get("uid", ""),
            "X-Enterprise-Id": account.get("enterpriseId", ""),
            "X-Tenant-Id": account.get("enterpriseId", ""),
            "X-Domain": domain,
            "User-Agent": USER_AGENT,
        }
        return h

    def get_headers(self) -> dict:
        """返回带最新 token 的后端请求 header；必要时先刷新。"""
        with self._lock:
            if self._is_expired():
                self._refresh()
            s = self._session()
            return self._build_headers_from(s.get("auth") or {}, s.get("account") or {})

    def summary(self) -> dict:
        s = self._session()
        auth = s.get("auth") or {}
        acct = s.get("account") or {}
        exp = auth.get("expiresAt", 0)
        return {
            "uid": acct.get("uid"),
            "nickname": acct.get("nickname"),
            "enterpriseName": acct.get("enterpriseName"),
            "token_expires_at": exp,
            "token_expired": self._is_expired(),
        }


# ---------------------------------------------------------------------------
# 模型列表
# ---------------------------------------------------------------------------

DEFAULT_MODELS = [
    "default", "glm-5.1", "glm-5.0", "glm-5.0-turbo", "glm-5v-turbo",
    "glm-4.7", "glm-4.6", "glm-4.6v",
    "kimi-k2.6", "kimi-k2.5", "kimi-k2-thinking",
    "deepseek-v4-pro", "deepseek-v4-flash",
    "minimax-m2.5", "minimax-m2.7", "minimax-m3-play",
    "hy3-preview-agent", "hunyuan-chat", "hunyuan-2.0-thinking", "auto",
]

# 后端请求体里出现过的额外字段（透传时若客户端给了就保留）
PASSTHROUGH_BODY_KEYS = {
    "model", "messages", "tools", "tool_choice", "temperature",
    "max_tokens", "max_completion_tokens", "top_p", "stream",
    "stream_options", "stop", "presence_penalty", "frequency_penalty",
    "n", "response_format", "seed", "user", "reasoning_effort",
    "verbosity", "reasoning_summary",
}

# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------

app = FastAPI(title="codebuddy2openai", version="2.0")
CONFIG: dict = {"api_key": "", "creds": {}, "cred_order": [], "log_path": None,
                "desensitize": False}  # creds: {nickname: CredentialManager}


class UpstreamHTTPException(HTTPException):
    pass


@app.exception_handler(UpstreamHTTPException)
async def upstream_exception_handler(_request: Request, exc: UpstreamHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.detail,
        headers=exc.headers,
    )


# ---------------------------------------------------------------------------
# 日志（写文件）
# ---------------------------------------------------------------------------

_LOG_LOCK = threading.Lock()


def _log(msg: str):
    """写一行日志到 CONFIG['log_path'] 指定的文件（追加，带时间戳）。未设置则丢弃。"""
    path = CONFIG.get("log_path")
    if not path:
        return
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    try:
        with _LOG_LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
    except OSError:
        pass  # 日志失败不应影响主流程




def _truncate(s: str, n: int = 80) -> str:
    s = str(s).replace("\n", " ").strip()
    return s[:n] + ("…" if len(s) > n else "")


def _check_auth(authorization: Optional[str], x_api_key: Optional[str]):
    key = CONFIG["api_key"]
    if not key:
        return
    token = ""
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()
    if not token and x_api_key:
        token = x_api_key
    if token != key:
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key", "type": "auth_error"}})


def _parse_model_account(model_str: str) -> tuple[str, str | None]:
    """解析 'deepseek-v4-flash@account1' → ('deepseek-v4-flash', 'account1')"""
    if "@" in model_str:
        parts = model_str.rsplit("@", 1)
        return parts[0], parts[1]
    return model_str, None


def _cred(account: str | None = None) -> CredentialManager:
    creds: dict = CONFIG["creds"]
    order: list = CONFIG["cred_order"]
    if not creds:
        raise HTTPException(status_code=503, detail={"error": {"message": "未找到登录凭据，请先在桌面端登录 CodeBuddy/WorkBuddy", "type": "auth_error"}})
    if account and account in creds:
        return creds[account]
    if account and account not in creds:
        raise HTTPException(status_code=400, detail={"error": {"message": f"账号 '{account}' 不存在，可用账号: {list(creds.keys())}", "type": "invalid_request_error"}})
    return creds[order[0]]


@app.get("/health")
def health():
    creds: dict = CONFIG["creds"]
    auth_file = "、".join(str(p) for p in creds.values()) if creds else "(未找到)"
    info: dict = {"status": "ok", "platform": sys.platform, "python": sys.version.split()[0],
                  "auth_files": auth_file, "mode": "direct-proxy (native function calling)",
                  "accounts": list(CONFIG["cred_order"])}
    if creds:
        info["credentials"] = {}
        for name, cm in creds.items():
            try:
                info["credentials"][name] = cm.summary()
            except Exception as e:
                info["credentials"][name] = {"error": str(e)}
    return info


@app.get("/v1/models")
def list_models(authorization: Optional[str] = Header(default=None),
                x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_auth(authorization, x_api_key)
    accounts = CONFIG["cred_order"]
    data = []
    for m in DEFAULT_MODELS:
        if accounts:
            for acct in accounts:
                data.append({"id": f"{m}@{acct}", "object": "model", "created": 1700000000, "owned_by": "codebuddy"})
        else:
            data.append({"id": m, "object": "model", "created": 1700000000, "owned_by": "codebuddy"})
    return {"object": "list", "data": data}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request,
                           authorization: Optional[str] = Header(default=None),
                           x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_auth(authorization, x_api_key)

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"bad json: {e}", "type": "invalid_request_error"}})

    messages = payload.get("messages") or []
    if not messages:
        raise HTTPException(status_code=400, detail={"error": {"message": "messages is required", "type": "invalid_request_error"}})

    raw_model = payload.get("model", "auto")
    model_name, account = _parse_model_account(raw_model)
    cred = _cred(account)

    # 构造后端 body：只透传已知的合法字段
    client_wants_stream = bool(payload.get("stream"))
    body = {k: payload[k] for k in PASSTHROUGH_BODY_KEYS if k in payload}
    body["model"] = model_name
    body["messages"] = _normalize_chat_messages(messages)
    # 后端只支持流式：始终以 stream=True 调后端，非流式由转换器聚合
    body["stream"] = True
    if "stream_options" not in body:
        body["stream_options"] = {"include_usage": True}

    # 可选：脱敏。缓解客户端合规模板（如 ZCode 的 system 声明）被后端误判为敏感词。
    # 只对 system 角色消息里的"合规声明高频词"插入零宽空格，不改用户输入。
    if CONFIG.get("desensitize"):
        body = desensitize_body(body, roles=("system",), include_tools=True)

    # 日志：请求摘要
    model_name = payload.get("model", "auto")
    tool_names = [t.get("function", {}).get("name") for t in (payload.get("tools") or [])
                  if isinstance(t, dict)]
    last_user = _last_user_text(messages)
    rid = os.urandom(4).hex()
    _log(f"[{rid}] ▶ REQUEST {model_name} | stream={client_wants_stream} | msgs={len(messages)}"
         + (f" | tools={tool_names}" if tool_names else "")
         + (f" | last_user={_truncate(last_user, 60)!r}" if last_user else ""))
    # 完整请求体（发往后端的实际内容；若启用脱敏，这里已是脱敏后）
    _log(f"[{rid}] ── REQUEST BODY (发往后端) ──\n{json.dumps(body, ensure_ascii=False, indent=2)}")

    headers = cred.get_headers()
    url = f"{BACKEND}/v2/chat/completions"
    t0 = time.time()

    if client_wants_stream:
        return StreamingResponse(
            _stream_upstream(url, headers, body, model_name, t0, rid),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 非流式：后端只支持流式，这里把后端 SSE 聚合成单个 chat.completion 响应
    try:
        async with httpx.AsyncClient(timeout=300) as c:
            async with c.stream("POST", url, headers=headers, json=body) as r:
                if r.status_code != 200:
                    raw = await r.aread()
                    _log(f"[{rid}] ✗ HTTP {r.status_code} | {model_name} | {_truncate(raw.decode('utf-8','replace'),200)}")
                    _log(f"[{rid}] ── ERROR BODY ──\n{raw.decode('utf-8','replace')}")
                    raise UpstreamHTTPException(
                        status_code=r.status_code,
                        detail=_safe_err_raw(raw, r.status_code),
                    )
                collected = await _collect_stream(r)
    except HTTPException:
        raise
    except httpx.HTTPError as e:
        _log(f"[{rid}] ✗ 网络错误 | {model_name} | {e}")
        raise UpstreamHTTPException(
            status_code=502,
            detail={"error": {
                "message": f"upstream error: {e}",
                "type": "upstream_error",
                "code": 502,
                "status": 502,
            }},
        )
    _log_finish(model_name, t0, collected, rid)
    return JSONResponse(content=collected)


# ---------------------------------------------------------------------------
# Responses API (v1/responses) compatibility adapter
# ---------------------------------------------------------------------------

def _normalize_chat_messages(messages: list) -> list:
    """Return an upstream-compatible copy without changing message content."""
    normalized = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        item = dict(message)
        if item.get("role") == "developer":
            item["role"] = "system"
        normalized.append(item)
    return normalized


def _response_content_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)
    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") in {
            "input_text", "output_text", "text"
        }:
            parts.append(str(block.get("text", "")))
    return "\n".join(part for part in parts if part)


def _response_content_parts(content) -> list | None:
    """把 Responses 内容块转成 Chat Completions 内容块（保留图片）。
    无图片时返回 None，调用方走原来的纯文本路径，行为不变。"""
    if isinstance(content, str) or not isinstance(content, list):
        return None
    parts = []
    has_image = False
    for block in content:
        if isinstance(block, str):
            parts.append({"type": "text", "text": block})
        elif isinstance(block, dict):
            btype = block.get("type")
            if btype in {"input_text", "output_text", "text"}:
                parts.append({"type": "text", "text": str(block.get("text", ""))})
            elif btype == "input_image":
                url = block.get("image_url")
                if isinstance(url, dict):
                    url = url.get("url")
                if url:
                    part = {"type": "image_url", "image_url": {"url": url}}
                    if block.get("detail"):
                        part["image_url"]["detail"] = block["detail"]
                    parts.append(part)
                    has_image = True
    return parts if has_image else None


def _responses_msg_to_chat(item: dict) -> dict:
    """Convert one Responses message item to Chat Completions format."""
    role_map = {
        "developer": "system",
        "assistant": "assistant",
        "user": "user",
        "system": "system",
    }
    role = role_map.get(item.get("role", "user"), "user")
    parts = _response_content_parts(item.get("content"))
    if parts is not None:
        return {"role": role, "content": parts}
    return {"role": role, "content": _response_content_text(item.get("content"))}


def _json_string(value, default: str = "") -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return default
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _responses_input_to_messages(input_data, instructions: str | None = None) -> list:
    """Convert Responses messages and tool items to a Chat Completions history."""
    items = input_data if isinstance(input_data, list) else [input_data]
    messages: list[dict] = []
    pending_calls: list[dict] = []

    def flush_calls():
        if not pending_calls:
            return
        if messages and messages[-1].get("role") == "assistant" and not messages[-1].get("tool_calls"):
            messages[-1]["tool_calls"] = list(pending_calls)
        else:
            messages.append({"role": "assistant", "content": None, "tool_calls": list(pending_calls)})
        pending_calls.clear()

    for item in items:
        if isinstance(item, str):
            flush_calls()
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            continue

        item_type = item.get("type", "message" if item.get("role") else "")
        if item_type == "message" or item.get("role") in {
            "developer", "system", "user", "assistant"
        }:
            flush_calls()
            messages.append(_responses_msg_to_chat(item))
        elif item_type in {"function_call", "custom_tool_call"}:
            call_id = item.get("call_id") or item.get("id") or "call_" + os.urandom(8).hex()
            arguments = item.get("arguments", "{}")
            if item_type == "custom_tool_call":
                arguments = {"input": item.get("input", "")}
            pending_calls.append({
                "id": call_id,
                "type": "function",
                "function": {
                    "name": str(item.get("name", "")),
                    "arguments": _json_string(arguments, "{}"),
                },
            })
        elif item_type in {"function_call_output", "custom_tool_call_output"}:
            flush_calls()
            output = item.get("output", "")
            # 如果 output 是列表且包含图片，保留结构化内容
            if isinstance(output, list):
                parts = []
                for blk in output:
                    if isinstance(blk, dict) and blk.get("type") == "input_image":
                        url = blk.get("image_url")
                        if isinstance(url, dict):
                            url = url.get("url")
                        if url:
                            parts.append({"type": "image_url", "image_url": {"url": url}})
                    elif isinstance(blk, dict) and blk.get("type") in {"input_text", "output_text", "text"}:
                        parts.append({"type": "text", "text": str(blk.get("text", ""))})
                    elif isinstance(blk, str):
                        parts.append({"type": "text", "text": blk})
                if parts:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": item.get("call_id") or item.get("id") or "",
                        "content": parts,
                    })
                    continue
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("call_id") or item.get("id") or "",
                "content": _json_string(output),
            })
    flush_calls()
    if instructions:
        messages.insert(0, {"role": "system", "content": instructions})
    return messages


def _responses_tools_to_chat(tools) -> tuple[list[dict], set[str]]:
    """Convert Responses function/custom tools to Chat Completions tools."""
    converted = []
    custom_names: set[str] = set()
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        tool_type = tool.get("type")
        if tool_type == "function" and isinstance(tool.get("function"), dict):
            converted.append(tool)
            continue
        if tool_type not in {"function", "custom"}:
            continue
        name = str(tool.get("name", ""))
        if not name:
            continue
        function = {
            "name": name,
            "description": str(tool.get("description", "")),
        }
        if tool_type == "custom":
            custom_names.add(name)
            function["parameters"] = {
                "type": "object",
                "properties": {"input": {"type": "string"}},
                "required": ["input"],
                "additionalProperties": False,
            }
        else:
            function["parameters"] = tool.get("parameters") or {
                "type": "object", "properties": {}
            }
            if "strict" in tool:
                function["strict"] = bool(tool["strict"])
        converted.append({"type": "function", "function": function})
    return converted, custom_names


def _responses_tool_choice_to_chat(choice):
    if not isinstance(choice, dict):
        return choice
    if choice.get("type") in {"function", "custom"} and choice.get("name"):
        return {"type": "function", "function": {"name": choice["name"]}}
    return "auto"


def _responses_request_to_chat(payload: dict, model: str) -> tuple[dict, set[str]]:
    messages = _responses_input_to_messages(
        payload.get("input", ""), payload.get("instructions")
    )
    body = {
        "model": model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    for key in ("temperature", "top_p", "parallel_tool_calls"):
        if key in payload:
            body[key] = payload[key]
    if payload.get("max_output_tokens") is not None:
        body["max_completion_tokens"] = payload["max_output_tokens"]
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("effort"):
        body["reasoning_effort"] = reasoning["effort"]

    tools, custom_names = _responses_tools_to_chat(payload.get("tools"))
    if tools:
        body["tools"] = tools
    if payload.get("tool_choice") is not None:
        body["tool_choice"] = _responses_tool_choice_to_chat(payload["tool_choice"])
    return body, custom_names


def _usage_to_responses(usage: dict) -> dict:
    """将 Chat Completions usage 转为 Responses API usage 格式。"""
    input_tokens = usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0)
    output_tokens = usage.get("completion_tokens", 0) or usage.get("output_tokens", 0)
    cached_tokens = usage.get("cached_tokens", 0)
    prompt_details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or usage.get("output_tokens_details") or {}
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": {
            "cached_tokens": prompt_details.get("cached_tokens", cached_tokens) or 0,
        },
        "output_tokens": output_tokens,
        "output_tokens_details": {
            "reasoning_tokens": completion_details.get("reasoning_tokens", 0) or 0,
        },
        "total_tokens": usage.get("total_tokens", 0) or input_tokens + output_tokens,
    }


def _custom_tool_input(arguments: str) -> str:
    try:
        value = json.loads(arguments)
    except (TypeError, json.JSONDecodeError):
        return arguments or ""
    if isinstance(value, dict) and isinstance(value.get("input"), str):
        return value["input"]
    return _json_string(value)


def _response_object(
    response_id: str,
    model: str,
    created_at: int,
    status: str,
    output: list,
    usage: dict | None,
) -> dict:
    return {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": status,
        "completed_at": int(time.time()) if status == "completed" else None,
        "error": None,
        "incomplete_details": None if status == "completed" else {"reason": "content_filter"},
        "instructions": None,
        "model": model,
        "output": output,
        "parallel_tool_calls": True,
        "previous_response_id": None,
        "reasoning": {"effort": None, "summary": None},
        "store": False,
        "temperature": None,
        "text": {"format": {"type": "text"}},
        "tool_choice": "auto",
        "tools": [],
        "top_p": None,
        "truncation": "disabled",
        "usage": usage,
    }


def _chat_completion_to_responses(
    response: dict, model: str, custom_tool_names: set[str] | None = None
) -> dict:
    """将 Chat Completions 响应转为 Responses API 格式。"""
    custom_tool_names = custom_tool_names or set()
    choices = response.get("choices", [{}])
    choice = choices[0] if choices else {}
    msg = choice.get("message", {})
    finish = choice.get("finish_reason", "stop")
    output = []
    text = msg.get("content") or ""
    if text:
        output.append({
            "type": "message",
            "id": "msg_" + os.urandom(12).hex(),
            "status": "completed",
            "role": "assistant",
            "content": [{
                "type": "output_text", "text": text,
                "annotations": [], "logprobs": [],
            }],
        })

    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        name = str(fn.get("name", ""))
        arguments = str(fn.get("arguments", "{}"))
        call_id = tc.get("id") or "call_" + os.urandom(8).hex()
        if name in custom_tool_names:
            output.append({
                "type": "custom_tool_call",
                "id": "ctc_" + os.urandom(12).hex(),
                "call_id": call_id,
                "name": name,
                "input": _custom_tool_input(arguments),
                "status": "completed",
            })
        else:
            output.append({
                "type": "function_call",
                "id": "fc_" + os.urandom(12).hex(),
                "call_id": call_id,
                "name": name,
                "arguments": arguments,
                "status": "completed",
            })

    status = "incomplete" if finish == "content-filter" else "completed"
    return _response_object(
        "resp_" + os.urandom(12).hex(), model, int(time.time()), status,
        output, _usage_to_responses(response.get("usage", {})),
    )


@app.post("/v1/responses")
async def responses_api(request: Request,
                        authorization: Optional[str] = Header(default=None),
                        x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """OpenAI Responses API compatibility endpoint."""
    _check_auth(authorization, x_api_key)

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail={
            "error": {"message": f"bad json: {e}", "type": "invalid_request_error"}
        })

    raw_model = payload.get("model", "auto")
    model, account = _parse_model_account(raw_model)
    cred = _cred(account)
    client_wants_stream = bool(payload.get("stream"))
    body, custom_tool_names = _responses_request_to_chat(payload, model)
    if not body["messages"]:
        raise HTTPException(status_code=400, detail={
            "error": {"message": "input is required", "type": "invalid_request_error"}
        })

    # 可选脱敏
    if CONFIG.get("desensitize"):
        body = desensitize_body(body, roles=("system",), include_tools=True)

    headers = cred.get_headers()
    url = f"{BACKEND}/v2/chat/completions"
    t0 = time.time()
    rid = os.urandom(4).hex()

    _log(f"[{rid}] ▶ RESPONSES {model} | stream={client_wants_stream} | input_type={type(payload.get('input')).__name__}")

    if client_wants_stream:
        return StreamingResponse(
            _responses_stream(
                url, headers, body, model, t0, rid,
                custom_tool_names=custom_tool_names,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 非流式：聚合并转格式
    try:
        async with httpx.AsyncClient(timeout=300) as c:
            async with c.stream("POST", url, headers=headers, json=body) as r:
                if r.status_code != 200:
                    raw = await r.aread()
                    _log(f"[{rid}] ✗ HTTP {r.status_code} | {model} | {_truncate(raw.decode('utf-8','replace'),200)}")
                    raise UpstreamHTTPException(
                        status_code=r.status_code,
                        detail=_safe_err_raw(raw, r.status_code),
                    )
                collected = await _collect_stream(r)
    except HTTPException:
        raise
    except httpx.HTTPError as e:
        _log(f"[{rid}] ✗ 网络错误 | {model} | {e}")
        raise UpstreamHTTPException(
            status_code=502,
            detail={"error": {
                "message": f"upstream error: {e}",
                "type": "upstream_error",
                "code": 502,
                "status": 502,
            }},
        )

    cc_response = _chat_completion_to_responses(collected, model, custom_tool_names)
    _log(f"[{rid}] ◀ RESPONSES {model} | {time.time()-t0:.1f}s | tokens={collected.get('usage',{}).get('total_tokens','?')}")
    return JSONResponse(content=cc_response)


def _responses_sse_event(payload: dict, sequence_number: int) -> str:
    event = dict(payload)
    event.setdefault("sequence_number", sequence_number)
    event_type = event.get("type", "message")
    return f"event: {event_type}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


async def _responses_stream(
    url: str,
    headers: dict,
    body: dict,
    model_name: str = "?",
    t0: float = 0.0,
    rid: str = "",
    custom_tool_names: set[str] | None = None,
    usage_callback=None,
):
    """Convert upstream Chat Completions SSE to typed Responses events."""
    prefix = f"[{rid}] " if rid else ""
    resp_id = "resp_" + os.urandom(12).hex()
    created_at = int(time.time())
    custom_tool_names = custom_tool_names or set()
    full_text = ""
    finish_reason = "stop"
    usage = {}
    saw_filter = False
    sequence = 0
    output_items: list[dict] = []
    text_state: dict | None = None
    tool_states: dict[int, dict] = {}
    line_buffer = ""

    def event(payload: dict) -> str:
        nonlocal sequence
        value = _responses_sse_event(payload, sequence)
        sequence += 1
        return value

    initial = _response_object(
        resp_id, model_name, created_at, "in_progress", [], None
    )
    yield event({"type": "response.created", "response": initial})
    yield event({"type": "response.in_progress", "response": initial})

    try:
        async with httpx.AsyncClient(timeout=None) as c:
            async with c.stream("POST", url, headers=headers, json=body) as r:
                if r.status_code != 200:
                    err = await r.aread()
                    detail = _safe_err_raw(err, r.status_code)["error"]
                    yield event({
                        "type": "error",
                        "code": str(detail.get("code") or "upstream_error"),
                        "message": detail["message"],
                        "param": None,
                        "status": r.status_code,
                    })
                    failed = _response_object(
                        resp_id, model_name, created_at, "failed", [], None
                    )
                    failed["error"] = {
                        "code": str(detail.get("code") or "upstream_error"),
                        "message": detail["message"],
                    }
                    failed["incomplete_details"] = None
                    yield event({"type": "response.failed", "response": failed})
                    return
                async for chunk in r.aiter_bytes():
                    if not chunk:
                        continue
                    line_buffer += chunk.decode("utf-8", "replace")
                    while "\n" in line_buffer:
                        line, line_buffer = line_buffer.split("\n", 1)
                        line = line.strip()
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            continue
                        try:
                            obj = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        if obj.get("usage"):
                            usage.update(obj["usage"])
                        # 检测审核拦截
                        data_str = json.dumps(obj, ensure_ascii=False)
                        if "content-filter" in data_str or "敏感" in data_str or "审核" in data_str:
                            saw_filter = True
                        for ch in obj.get("choices") or []:
                            if ch.get("finish_reason"):
                                finish_reason = ch["finish_reason"]
                            delta = ch.get("delta") or {}
                            text_chunk = delta.get("content", "")
                            if text_chunk:
                                if text_state is None:
                                    output_index = len(output_items)
                                    msg_id = "msg_" + os.urandom(12).hex()
                                    text_state = {"id": msg_id, "output_index": output_index}
                                    output_items.append({
                                        "id": msg_id, "type": "message",
                                        "status": "in_progress", "role": "assistant",
                                        "content": [],
                                    })
                                    yield event({
                                        "type": "response.output_item.added",
                                        "output_index": output_index,
                                        "item": output_items[-1],
                                    })
                                    yield event({
                                        "type": "response.content_part.added",
                                        "item_id": msg_id,
                                        "output_index": output_index,
                                        "content_index": 0,
                                        "part": {"type": "output_text", "text": "", "annotations": []},
                                    })
                                full_text += text_chunk
                                yield event({
                                    "type": "response.output_text.delta",
                                    "delta": text_chunk,
                                    "item_id": text_state["id"],
                                    "output_index": text_state["output_index"],
                                    "content_index": 0,
                                })
                            for tc in delta.get("tool_calls") or []:
                                index = int(tc.get("index", 0))
                                state = tool_states.setdefault(index, {
                                    "name": "", "arguments": "", "call_id": "",
                                    "item_id": "", "output_index": None,
                                })
                                if tc.get("id"):
                                    state["call_id"] = tc["id"]
                                function = tc.get("function") or {}
                                if function.get("name"):
                                    state["name"] = function["name"]
                                argument_delta = function.get("arguments") or ""
                                if state["output_index"] is None and state["name"]:
                                    state["output_index"] = len(output_items)
                                    is_custom = state["name"] in custom_tool_names
                                    state["is_custom"] = is_custom
                                    state["item_id"] = (
                                        "ctc_" if is_custom else "fc_"
                                    ) + os.urandom(12).hex()
                                    if not state["call_id"]:
                                        state["call_id"] = "call_" + os.urandom(8).hex()
                                    item = {
                                        "id": state["item_id"],
                                        "type": "custom_tool_call" if is_custom else "function_call",
                                        "status": "in_progress",
                                        "call_id": state["call_id"],
                                        "name": state["name"],
                                    }
                                    item["input" if is_custom else "arguments"] = ""
                                    output_items.append(item)
                                    yield event({
                                        "type": "response.output_item.added",
                                        "output_index": state["output_index"],
                                        "item": item,
                                    })
                                state["arguments"] += argument_delta
                                if argument_delta and state.get("output_index") is not None and not state.get("is_custom"):
                                    yield event({
                                        "type": "response.function_call_arguments.delta",
                                        "item_id": state["item_id"],
                                        "output_index": state["output_index"],
                                        "delta": argument_delta,
                                    })
    except httpx.HTTPError as e:
        # 上游流中途断开：除了 error 事件，还必须补发 response.failed 终态事件，
        # 否则客户端会一直等 response.completed，报 "stream closed before response.completed"
        yield event({
            "type": "error", "code": "upstream_error",
            "message": str(e), "param": None,
        })
        failed = _response_object(
            resp_id, model_name, created_at, "failed",
            output_items, _usage_to_responses(usage),
        )
        yield event({"type": "response.failed", "response": failed})
        return
    except Exception as e:
        # 兜底：转换层自身异常同样优雅收尾，避免客户端干等终态事件
        yield event({
            "type": "error", "code": "converter_error",
            "message": str(e)[:500], "param": None,
        })
        failed = _response_object(
            resp_id, model_name, created_at, "failed",
            output_items, _usage_to_responses(usage),
        )
        yield event({"type": "response.failed", "response": failed})
        return

    if text_state is not None:
        text_item = output_items[text_state["output_index"]]
        part = {
            "type": "output_text", "text": full_text,
            "annotations": [], "logprobs": [],
        }
        yield event({
            "type": "response.output_text.done", "text": full_text,
            "item_id": text_state["id"],
            "output_index": text_state["output_index"], "content_index": 0,
        })
        yield event({
            "type": "response.content_part.done", "part": part,
            "item_id": text_state["id"],
            "output_index": text_state["output_index"], "content_index": 0,
        })
        text_item["status"] = "completed"
        text_item["content"] = [part]
        yield event({
            "type": "response.output_item.done",
            "output_index": text_state["output_index"], "item": text_item,
        })

    for index in sorted(tool_states):
        state = tool_states[index]
        if state.get("output_index") is None:
            continue
        item = output_items[state["output_index"]]
        if state.get("is_custom"):
            tool_input = _custom_tool_input(state["arguments"])
            if tool_input:
                yield event({
                    "type": "response.custom_tool_call_input.delta",
                    "item_id": state["item_id"],
                    "output_index": state["output_index"], "delta": tool_input,
                })
            yield event({
                "type": "response.custom_tool_call_input.done",
                "item_id": state["item_id"],
                "output_index": state["output_index"], "input": tool_input,
            })
            item["input"] = tool_input
        else:
            yield event({
                "type": "response.function_call_arguments.done",
                "item_id": state["item_id"],
                "output_index": state["output_index"],
                "arguments": state["arguments"],
            })
            item["arguments"] = state["arguments"]
        item["status"] = "completed"
        yield event({
            "type": "response.output_item.done",
            "output_index": state["output_index"], "item": item,
        })

    status = "incomplete" if finish_reason == "content-filter" else "completed"
    final_response = _response_object(
        resp_id, model_name, created_at, status,
        output_items, _usage_to_responses(usage),
    )
    if usage_callback is not None:
        usage_callback(dict(usage))
    final_type = "response.incomplete" if status == "incomplete" else "response.completed"
    yield event({"type": final_type, "response": final_response})

    elapsed = time.time() - t0 if t0 else 0
    tag = " ⚠️内容审核拦截" if (saw_filter or finish_reason == "content-filter") else ""
    _log(f"{prefix}◀ RESPONSES {model_name} | {elapsed:.1f}s | finish={finish_reason}{tag} | tokens={usage.get('total_tokens', '?')}")


def _last_user_text(messages: list) -> str:
    """取最后一条 user 消息的文本，用于日志预览。"""
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        content = m.get("content", "")
        if isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    return str(blk.get("text", ""))
            return ""
        return str(content)
    return ""


def _log_finish(model_name: str, t0: float, result: dict, rid: str = ""):
    """记录一次完成的请求：耗时 / finish_reason / usage / 工具调用 / 审核拦截 + 完整响应。"""
    elapsed = time.time() - t0
    prefix = f"[{rid}] " if rid else ""
    choice = (result.get("choices") or [{}])[0]
    finish = choice.get("finish_reason")
    msg = choice.get("message") or {}
    tcs = msg.get("tool_calls") or []
    usage = result.get("usage") or {}
    tag = ""
    if finish == "content-filter":
        tag = " ⚠️内容审核拦截"
    tc_names = [t.get("function", {}).get("name") for t in tcs]
    _log(f"{prefix}◀ RESPONSE {model_name} | {elapsed:.1f}s | finish={finish}{tag}"
         + (f" | tool_calls={tc_names}" if tc_names else "")
         + f" | tokens={usage.get('total_tokens', '?')}")
    # 完整响应体
    _log(f"{prefix}── RESPONSE BODY ──\n{json.dumps(result, ensure_ascii=False, indent=2)}")


async def _collect_stream(response: httpx.Response) -> dict:
    """消费后端的 OpenAI SSE 流，聚合成单个非流式 chat.completion 对象。

    合并所有 chunk 的 delta（content / tool_calls），并取 usage / finish_reason。
    """
    content_parts: list[str] = []
    # tool_calls: index -> {id, name, arguments(分片拼接)}
    tool_calls: dict[int, dict] = {}
    model: str | None = None
    finish_reason: str | None = None
    usage: dict | None = None

    async for line in response.aiter_lines():
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        model = chunk.get("model") or model
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content_parts.append(delta["content"])
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = tool_calls.setdefault(idx, {"id": None, "name": None, "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]

    tcs = None
    if tool_calls:
        tcs = [
            {"id": v["id"], "type": "function",
             "function": {"name": v["name"], "arguments": v["arguments"]}}
            for _, v in sorted(tool_calls.items())
        ]
        finish_reason = finish_reason or "tool_calls"

    message = {"role": "assistant", "content": "".join(content_parts) or None}
    if tcs:
        message["tool_calls"] = tcs
    return {
        "id": "chatcmpl-" + os.urandom(12).hex(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model or "unknown",
        "choices": [{"index": 0, "message": message,
                     "finish_reason": finish_reason or "stop"}],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _safe_err_raw(raw: bytes, status: int) -> dict:
    text = raw.decode("utf-8", "replace")
    try:
        payload = json.loads(text)
    except Exception:
        payload = None

    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        error = dict(payload["error"])
        error.setdefault("message", payload.get("msg") or f"upstream HTTP {status}")
        error.setdefault("type", "upstream_error")
        error.setdefault("code", payload.get("code", status))
        error.setdefault("status", status)
        return {"error": error}

    if isinstance(payload, dict):
        message = payload.get("msg") or payload.get("message") or text[:500]
        code = payload.get("code", status)
        request_id = payload.get("requestId") or payload.get("request_id")
    else:
        message = text[:500] or f"upstream HTTP {status}"
        code = status
        request_id = None

    error = {
        "message": str(message)[:500],
        "type": "upstream_error",
        "code": code,
        "status": status,
    }
    if request_id:
        error["request_id"] = request_id
    return {"error": error}


async def _stream_upstream(url: str, headers: dict, body: dict,
                           model_name: str = "?", t0: float = 0.0, rid: str = ""):
    """把后端 SSE 原样转发给客户端（后端已是标准 OpenAI SSE，含 tool_calls）。

    同时轻量解析流，统计 finish_reason / tool_calls / usage 用于日志，不阻塞转发。
    完整原始 SSE 累积后落盘到日志（调试用）。
    """
    finish_reason = None
    tool_names: list[str] = []
    usage: dict = {}
    saw_filter = False
    buf = b""
    raw_parts: list[bytes] = []   # 累积完整原始 SSE
    prefix = f"[{rid}] " if rid else ""

    def _feed(chunk: bytes):
        nonlocal finish_reason, saw_filter, buf
        # 行缓冲解析：把累计的 chunk 按 data: 行切出来统计
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                continue
            try:
                obj = json.loads(data)
            except Exception:
                continue
            if obj.get("usage"):
                usage.update(obj["usage"])
            for ch in obj.get("choices") or []:
                if ch.get("finish_reason"):
                    finish_reason = ch["finish_reason"]
                for tc in (ch.get("delta") or {}).get("tool_calls") or []:
                    nm = (tc.get("function") or {}).get("name")
                    if nm:
                        tool_names.append(nm)
            # 内容审核拦截常以 content-filter 或特殊中文文案返回
            try:
                text_repr = data.decode("utf-8", "replace")
            except Exception:
                text_repr = ""
            if "content-filter" in text_repr or "敏感" in text_repr or "审核" in text_repr:
                saw_filter = True

    try:
        async with httpx.AsyncClient(timeout=None) as c:
            async with c.stream("POST", url, headers=headers, json=body) as r:
                if r.status_code != 200:
                    err = await r.aread()
                    _log(f"{prefix}✗ HTTP {r.status_code} | {model_name} | {_truncate(err.decode('utf-8','replace'),200)}")
                    _log(f"{prefix}── ERROR BODY ──\n{err.decode('utf-8','replace')}")
                    yield _err_event(err, r.status_code)
                    yield b"data: [DONE]\n\n"
                    return
                async for chunk in r.aiter_bytes():
                    if chunk:
                        raw_parts.append(chunk)
                        _feed(chunk)
                        yield chunk
    except httpx.HTTPError as e:
        _log(f"{prefix}✗ 网络错误 | {model_name} | {e}")
        yield _err_event(str(e).encode(), 502)
        yield b"data: [DONE]\n\n"  # 错误后补终止帧，让客户端干净收尾

    # 流结束：输出完成日志
    elapsed = time.time() - t0 if t0 else 0
    tag = " ⚠️内容审核拦截" if (saw_filter or finish_reason == "content-filter") else ""
    _log(f"{prefix}◀ RESPONSE {model_name} | {elapsed:.1f}s | stream finish={finish_reason}{tag}"
         + (f" | tool_calls={tool_names}" if tool_names else "")
         + f" | tokens={usage.get('total_tokens', '?')}")
    # 完整原始 SSE（后端返回的全部内容）
    _log(f"{prefix}── RESPONSE RAW SSE ──\n{b''.join(raw_parts).decode('utf-8','replace')}")


def _err_event(msg: bytes, status: int) -> bytes:
    chunk = _safe_err_raw(msg, status)
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

def preflight() -> bool:
    auth_files = find_auth_files()
    sys.stderr.write("==== 预检 ====\n")
    sys.stderr.write(f"平台      : {sys.platform}\n")
    sys.stderr.write(f"Python    : {sys.version.split()[0]}\n")
    sys.stderr.write(f"后端      : {BACKEND} (直连，原生 function calling)\n")
    sys.stderr.write(f"登录文件  : {len(auth_files)} 个\n")
    if auth_dirs():
        sys.stderr.write(f"已查目录  : {', '.join(str(d) for d in auth_dirs())}\n")
    ok = True
    if not auth_files:
        sys.stderr.write("\n[警告] 未找到登录文件。请在桌面端完成登录（CodeBuddy/WorkBuddy）。\n")
        ok = False
    else:
        creds = {}
        order = []
        for nickname, path in auth_files.items():
            try:
                cm = CredentialManager(path)
                info = cm.summary()
                creds[nickname] = cm
                order.append(nickname)
                sys.stderr.write(f"  [{nickname}] {info.get('nickname')} / {info.get('enterpriseName') or '个人'} | token过期={'是(将自动刷新)' if info['token_expired'] else '否'}\n")
            except Exception as e:
                sys.stderr.write(f"  [{nickname}] 读取凭据失败：{e}\n")
                ok = False
        CONFIG["creds"] = creds
        CONFIG["cred_order"] = order
    sys.stderr.write("================\n")
    return ok


def main():
    ap = argparse.ArgumentParser(description="CodeBuddy -> OpenAI 兼容转换器（直连后端）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--api-key", default=os.environ.get("CODEBUDDY2OPENAI_KEY", ""),
                    help="可选：要求客户端携带的 API key（默认不校验）")
    ap.add_argument("--log", default=None, metavar="PATH",
                    help="开启日志并写到该文件（如 --log converter.log 或 --log /tmp/cb.log）。"
                         "不传则不记日志。")
    ap.add_argument("--desensitize", action="store_true",
                    help="启用脱敏：对 system 消息里的合规模板敏感词（DoS/exploit/credential 等）"
                         "插入零宽空格，缓解被后端内容审核误拦。默认关闭。")
    ap.add_argument("--skip-check", action="store_true", help="跳过启动预检")
    args = ap.parse_args()

    CONFIG["api_key"] = args.api_key
    CONFIG["desensitize"] = args.desensitize
    # --log 直接指定文件路径即开启；不传则不记
    CONFIG["log_path"] = args.log if args.log else os.environ.get("CODEBUDDY2OPENAI_LOG")
    auth_files = find_auth_files()
    CONFIG["creds"] = {nickname: CredentialManager(path) for nickname, path in auth_files.items()} if auth_files else {}; CONFIG["cred_order"] = list(CONFIG["creds"].keys())

    if not args.skip_check:
        preflight()

    sys.stderr.write(f"\n✅ 监听 http://{args.host}:{args.port}（直连后端，原生 function calling）\n")
    sys.stderr.write("   GET  /v1/models\n")
    sys.stderr.write("   POST /v1/chat/completions   (原生 tools/tool_calls，支持流式)\n")
    sys.stderr.write("   POST /v1/responses          (Responses API compatibility)\n")
    sys.stderr.write("   GET  /health\n")
    if args.api_key:
        sys.stderr.write("   鉴权已启用（API key 已设置）\n")
    if CONFIG["log_path"]:
        sys.stderr.write(f"   日志      : {CONFIG['log_path']}\n")
    if args.desensitize:
        sys.stderr.write("   脱敏      : 已启用（system 合规词零宽处理）\n")
    sys.stderr.write("按 Ctrl+C 退出。\n\n")

    # 启动时写一条标记
    _log(f"==== converter 启动 ====")

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
