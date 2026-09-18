"""官方模型列表动态获取：定时从上游拉取用户可用模型，带 TTL 缓存。

官方上新模型后此列表自动更新，网关无需改代码。
"""
from __future__ import annotations

import threading
import time

import httpx

from converter import BACKEND, DEFAULT_MODELS

from . import state

OFFICIAL_MODELS_PATH = "/v2/enterprises/personal/models"
MODELS_CACHE_TTL = 3600  # 秒

_models_cache: dict = {"list": None, "ts": 0.0}
_models_lock = threading.Lock()


def _extract_official_models(payload: dict) -> list[str]:
    """从官方模型接口响应提取用户可用模型。

    优先取带 default 标签的 agent（cli）的 models；缺失时回退为
    全部 agent 模型的并集（剔除内部子代理用的 lite）。
    """
    agents = (payload.get("data") or {}).get("agents") or []
    for agent in agents:
        models = agent.get("models") or []
        tags = agent.get("tags") or []
        if models and "default" in tags:
            return list(dict.fromkeys(models))
    union: list[str] = []
    for agent in agents:
        for m in agent.get("models") or []:
            if m != "lite" and m not in union:
                union.append(m)
    return union


def fetch_official_models(force: bool = False) -> list[str] | None:
    """拉取官方模型列表（带 TTL 缓存）；失败时返回缓存或 None。"""
    now = time.time()
    with _models_lock:
        if (
            not force
            and _models_cache["list"]
            and now - _models_cache["ts"] < MODELS_CACHE_TTL
        ):
            return _models_cache["list"]
    for nickname in state.account_store.list_nicknames():
        cm = state.account_store.get_cm(nickname)
        if cm is None:
            continue
        try:
            headers = cm.get_headers()
            with httpx.Client(timeout=10) as client:
                resp = client.get(f"{BACKEND}{OFFICIAL_MODELS_PATH}", headers=headers)
            if resp.status_code != 200:
                continue
            models = _extract_official_models(resp.json())
            if models:
                with _models_lock:
                    _models_cache["list"] = models
                    _models_cache["ts"] = time.time()
                return models
        except Exception:
            continue
    with _models_lock:
        return _models_cache["list"]


def available_models() -> list[str]:
    """当前模型列表：官方动态列表优先，静态列表兜底追加。"""
    dynamic = fetch_official_models() or []
    return list(dynamic) + [m for m in DEFAULT_MODELS if m not in dynamic]