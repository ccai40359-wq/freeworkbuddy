"""OpenAI 兼容端点：/v1/models、/v1/chat/completions、/v1/responses，含 429 failover 与 credit 记账。"""
from __future__ import annotations

import json
import os
import time
from typing import Optional

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from converter import (
    BACKEND,
    DEFAULT_MODELS,
    PASSTHROUGH_BODY_KEYS,
    UpstreamHTTPException,
    _chat_completion_to_responses,
    _collect_stream,
    _log_finish,
    _normalize_chat_messages,
    _responses_request_to_chat,
    _responses_stream,
    _safe_err_raw,
    _stream_upstream,
    desensitize_body,
)

from .. import config, state
from ..logging_utils import log as _log

router = APIRouter()


def _api_key_entry(authorization: str | None, x_api_key: str | None) -> dict:
    token = ""
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()
    if not token and x_api_key:
        token = x_api_key
    entry = state.api_key_store.get(token)
    if not entry:
        raise HTTPException(status_code=401, detail={
            "error": {"message": "invalid api key", "type": "auth_error"}
        })
    if not entry.get("enabled", True):
        raise HTTPException(status_code=403, detail={
            "error": {"message": "api key disabled", "type": "auth_error"}
        })
    return entry


def _record_credit(nickname: str, usage: dict, rid: str,
                   key_entry: dict | None = None, model: str = ""):
    credit = usage.get("credit", 0) or usage.get("cached_tokens", 0) or 0
    if credit > 0:
        state.account_store.record_consumption(nickname, credit)
        state.usage_store.record(nickname, credit, model=model, key_entry=key_entry, rid=rid)
        _log(f"[{rid}] credit: {nickname} consumed {credit}")


async def _stream_with_credit(url: str, headers: dict, body: dict, model: str,
                              t0: float, rid: str, nickname: str,
                              key_entry: dict | None = None):
    """包装 _stream_upstream，拦截最后一条 chunk 记录 credit 消耗。"""
    last_chunk_text = None
    async for chunk in _stream_upstream(url, headers, body, model, t0, rid):
        last_chunk_text = chunk
        yield chunk
    # 从最后一条 chunk 提取 credit
    if last_chunk_text:
        try:
            text = last_chunk_text
            if isinstance(text, bytes):
                text = text.decode("utf-8", "replace")
            for line in text.split("\n"):
                line = line.strip()
                if line.startswith("data: ") and line != "data: [DONE]":
                    data = json.loads(line[6:])
                    usage = data.get("usage", {})
                    if usage:
                        credit = usage.get("credit", 0) or usage.get("cached_tokens", 0) or 0
                        if credit > 0:
                            state.account_store.record_consumption(nickname, credit)
                            state.usage_store.record(nickname, credit, model=model, key_entry=key_entry, rid=rid)
                            _log(f"[{rid}] credit: {nickname} consumed {credit}")
        except Exception:
            pass


async def _responses_stream_with_credit(
    url: str,
    headers: dict,
    body: dict,
    model: str,
    t0: float,
    rid: str,
    nickname: str,
    custom_tool_names: set,
    key_entry: dict | None = None,
):
    async for chunk in _responses_stream(
        url, headers, body, model, t0, rid,
        custom_tool_names=custom_tool_names,
        usage_callback=lambda usage: _record_credit(nickname, usage, rid, key_entry=key_entry, model=model),
    ):
        yield chunk


# ── 429 换号重试（failover）────────────────────────────────────────

FAILOVER_MAX_ATTEMPTS = 3  # 单请求最多尝试的账号数


def _is_429_chunk(chunk) -> bool:
    """判断流产出是否为上游 429 限流错误事件。

    兼容两种流：chat/completions（bytes，error.code==429）和
    responses（str，type==error 且 status==429 或文本含 429/too many）。
    """
    try:
        text = chunk.decode("utf-8", "replace") if isinstance(chunk, (bytes, bytearray)) else str(chunk)
    except Exception:
        return False
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        try:
            data = json.loads(line[5:].strip())
        except Exception:
            continue
        err = data.get("error")
        if isinstance(err, dict) and (
            err.get("code") == 429 or err.get("status") == 429
        ):
            return True
        if data.get("type") == "error":
            if data.get("status") == 429:
                return True
            blob = json.dumps(data, ensure_ascii=False).lower()
            if "429" in blob or "too many" in blob:
                return True
    return False


def _err_event(msg: bytes, status: int) -> bytes:
    chunk = _safe_err_raw(msg, status)
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")


async def _failover_stream(key_entry: dict, rid: str, make_stream):
    """429 换号重试包装：缓冲候选流的开头事件，若发现 429 错误事件则
    冷却该账号、丢弃缓冲、换号重开流；正常则先放缓冲再透传剩余。

    make_stream(nickname, cred) -> async generator
    """
    tried: set[str] = set()
    last_err = None
    for attempt in range(FAILOVER_MAX_ATTEMPTS):
        picked = state.rotator.pick(key_entry)
        if not picked or picked[0] in tried:
            break
        nickname, cred = picked
        tried.add(nickname)
        gen = make_stream(nickname, cred)
        buf: list = []
        hit_429 = False
        try:
            # responses 流的 429 错误事件是第 3 个产出，多探一个保险
            for _ in range(4):
                try:
                    chunk = await gen.__anext__()
                except StopAsyncIteration:
                    break
                buf.append(chunk)
                if _is_429_chunk(chunk):
                    hit_429 = True
                    break
        except Exception as e:
            await gen.aclose()
            state.rotator.mark_cooldown(nickname, 10)
            _log(f"[{rid}] 账号 {nickname} 连接异常，换号重试: {e}")
            last_err = _err_event(str(e).encode(), 502)
            continue
        if hit_429:
            await gen.aclose()
            state.rotator.mark_cooldown(nickname)
            _log(f"[{rid}] 上游 429 限流: {nickname} 冷却 60s，换号重试 ({attempt + 1}/{FAILOVER_MAX_ATTEMPTS})")
            last_err = buf[-1]
            continue
        for c in buf:
            yield c
        async for c in gen:
            yield c
        return
    if last_err is not None:
        yield last_err


@router.api_route("/v1", methods=["GET", "POST"])
def v1_root():
    """部分客户端会探测裸 /v1 路径。返回 200 使其通过探测，
    避免认证中间件 307 到 /login 后客户端跟随 POST 变成 405。"""
    return {"status": "ok", "endpoints": ["/v1/models", "/v1/chat/completions", "/v1/responses"]}


@router.get("/v1/models")
def list_models():
    """返回所有可用模型列表：官方动态列表优先，静态列表兜底追加。"""
    from ..models import available_models

    data = [{"id": m, "object": "model", "created": 1700000000, "owned_by": "codebuddy"}
            for m in available_models()]
    return {"object": "list", "data": data}


@router.post("/v1/chat/completions")
async def chat_completions(request: Request,
                           authorization: Optional[str] = Header(default=None),
                           x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    key_entry = _api_key_entry(authorization, x_api_key)

    # 选账号
    picked = state.rotator.pick(key_entry)
    if not picked:
        raise HTTPException(status_code=429, detail={"error": {"message": "所有账号已耗尽", "type": "rate_limit_error"}})
    nickname, cred = picked

    # 解析请求
    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"bad json: {e}", "type": "invalid_request_error"}})

    messages = payload.get("messages") or []
    if not messages:
        raise HTTPException(status_code=400, detail={"error": {"message": "messages is required", "type": "invalid_request_error"}})

    client_wants_stream = bool(payload.get("stream"))
    body = {k: payload[k] for k in PASSTHROUGH_BODY_KEYS if k in payload}
    body.setdefault("model", "auto")
    body["messages"] = _normalize_chat_messages(messages)
    body["stream"] = True
    if "stream_options" not in body:
        body["stream_options"] = {"include_usage": True}

    if config.CONFIG.get("desensitize"):
        body = desensitize_body(body, roles=("system",), include_tools=True)

    model_name = payload.get("model", "auto")
    headers = cred.get_headers()
    url = f"{BACKEND}/v2/chat/completions"
    t0 = time.time()
    rid = os.urandom(4).hex()

    _log(f"[{rid}] ▶ {nickname}/{model_name} | stream={client_wants_stream} | msgs={len(messages)}")

    if client_wants_stream:
        return StreamingResponse(
            _failover_stream(
                key_entry, rid,
                lambda nick, cred: _stream_with_credit(
                    url, cred.get_headers(), body, model_name, t0, rid, nick,
                    key_entry=key_entry),
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 非流式（429 自动换号重试）
    tried: set[str] = {nickname}
    collected = None
    last_status = 429
    last_detail = {"error": {"message": "所有账号已耗尽", "type": "rate_limit_error"}}
    for attempt in range(FAILOVER_MAX_ATTEMPTS):
        if attempt > 0:
            picked = state.rotator.pick(key_entry)
            if not picked or picked[0] in tried:
                break
            nickname, cred = picked
            headers = cred.get_headers()
            tried.add(nickname)
        try:
            async with httpx.AsyncClient(timeout=300) as c:
                async with c.stream("POST", url, headers=headers, json=body) as r:
                    if r.status_code == 429:
                        raw = await r.aread()
                        state.rotator.mark_cooldown(nickname)
                        _log(f"[{rid}] 上游 429 限流: {nickname} 冷却 60s，换号重试 ({attempt + 1}/{FAILOVER_MAX_ATTEMPTS})")
                        last_status, last_detail = 429, _safe_err_raw(raw, 429)
                        continue
                    if r.status_code != 200:
                        raw = await r.aread()
                        raise UpstreamHTTPException(
                            status_code=r.status_code,
                            detail=_safe_err_raw(raw, r.status_code),
                        )
                    collected = await _collect_stream(r)
        except HTTPException:
            raise
        except httpx.HTTPError as e:
            state.rotator.mark_cooldown(nickname, 10)
            _log(f"[{rid}] 账号 {nickname} 连接异常，换号重试: {e}")
            last_status, last_detail = 502, {"error": {"message": f"upstream error: {e}", "type": "upstream_error"}}
            continue
        break
    if collected is None:
        raise UpstreamHTTPException(status_code=last_status, detail=last_detail)

    # 记录额度消耗
    usage = collected.get("usage", {})
    _record_credit(nickname, usage, rid, key_entry=key_entry, model=model_name)

    _log_finish(model_name, t0, collected, rid)
    return JSONResponse(content=collected)


@router.post("/v1/responses")
async def responses_api(request: Request,
                        authorization: Optional[str] = Header(default=None),
                        x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """Responses API adapter for compatible model providers."""
    key_entry = _api_key_entry(authorization, x_api_key)
    picked = state.rotator.pick(key_entry)
    if not picked:
        raise HTTPException(status_code=429, detail={
            "error": {"message": "所有账号已耗尽", "type": "rate_limit_error"}
        })
    nickname, cred = picked

    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail={
            "error": {"message": f"bad json: {exc}", "type": "invalid_request_error"}
        })

    model_name = str(payload.get("model") or "auto")
    body, custom_tool_names = _responses_request_to_chat(payload, model_name)
    if not body["messages"]:
        raise HTTPException(status_code=400, detail={
            "error": {"message": "input is required", "type": "invalid_request_error"}
        })
    if config.CONFIG.get("desensitize"):
        body = desensitize_body(body, roles=("system",), include_tools=True)

    headers = cred.get_headers()
    url = f"{BACKEND}/v2/chat/completions"
    t0 = time.time()
    rid = os.urandom(4).hex()
    client_wants_stream = bool(payload.get("stream"))
    _log(
        f"[{rid}] ▶ RESPONSES {nickname}/{model_name} | "
        f"stream={client_wants_stream} | msgs={len(body['messages'])}"
    )

    if client_wants_stream:
        return StreamingResponse(
            _failover_stream(
                key_entry, rid,
                lambda nick, cred: _responses_stream_with_credit(
                    url, cred.get_headers(), body, model_name, t0, rid, nick,
                    custom_tool_names, key_entry=key_entry,
                ),
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 非流式（429 自动换号重试）
    tried: set[str] = {nickname}
    collected = None
    last_status = 429
    last_detail = {"error": {"message": "所有账号已耗尽", "type": "rate_limit_error"}}
    for attempt in range(FAILOVER_MAX_ATTEMPTS):
        if attempt > 0:
            picked = state.rotator.pick(key_entry)
            if not picked or picked[0] in tried:
                break
            nickname, cred = picked
            headers = cred.get_headers()
            tried.add(nickname)
        try:
            async with httpx.AsyncClient(timeout=300) as client:
                async with client.stream("POST", url, headers=headers, json=body) as response:
                    if response.status_code == 429:
                        raw = await response.aread()
                        state.rotator.mark_cooldown(nickname)
                        _log(f"[{rid}] 上游 429 限流: {nickname} 冷却 60s，换号重试 ({attempt + 1}/{FAILOVER_MAX_ATTEMPTS})")
                        last_status, last_detail = 429, _safe_err_raw(raw, 429)
                        continue
                    if response.status_code != 200:
                        raw = await response.aread()
                        raise UpstreamHTTPException(
                            status_code=response.status_code,
                            detail=_safe_err_raw(raw, response.status_code),
                        )
                    collected = await _collect_stream(response)
        except HTTPException:
            raise
        except httpx.HTTPError as exc:
            state.rotator.mark_cooldown(nickname, 10)
            _log(f"[{rid}] 账号 {nickname} 连接异常，换号重试: {exc}")
            last_status, last_detail = 502, {
                "error": {"message": f"upstream error: {exc}", "type": "upstream_error"}
            }
            continue
        break
    if collected is None:
        raise UpstreamHTTPException(status_code=last_status, detail=last_detail)

    usage = collected.get("usage") or {}
    _record_credit(nickname, usage, rid, key_entry=key_entry, model=model_name)
    result = _chat_completion_to_responses(
        collected, model_name, custom_tool_names
    )
    _log(
        f"[{rid}] ◀ RESPONSES {nickname}/{model_name} | "
        f"{time.time() - t0:.1f}s | tokens={usage.get('total_tokens', '?')}"
    )
    return JSONResponse(content=result)
