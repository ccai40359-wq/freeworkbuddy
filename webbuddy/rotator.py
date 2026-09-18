"""账号轮换策略：round-robin / queue / quota，带 429 冷却与可用性验证。"""
from __future__ import annotations

import threading
import time

from . import state
from .logging_utils import log as _log


class AccountRotator:
    """根据策略从账号池中选取下一个账号，自动检测账号是否可用。"""

    VERIFY_TTL = 60  # 验证结果缓存秒数
    COOLDOWN_TTL = 60  # 429 限流冷却秒数

    def __init__(self, account_store):
        self._store = account_store
        self._index: dict[str, int] = {}
        self._verified: dict[str, dict] = {}  # nickname -> {status, checked_at}
        self._cooldowns: dict[str, float] = {}  # nickname -> 冷却截止时刻（429 限流）
        self._lock = threading.Lock()

    def mark_cooldown(self, nickname: str, seconds: float | None = None):
        """账号被上游限流（429）时标记冷却，冷却期内 pick 跳过该账号。"""
        ttl = seconds if seconds is not None else self.COOLDOWN_TTL
        with self._lock:
            self._cooldowns[nickname] = time.time() + ttl

    def _in_cooldown(self, nickname: str, now: float) -> bool:
        until = self._cooldowns.get(nickname)
        if until is None:
            return False
        if now >= until:
            self._cooldowns.pop(nickname, None)
            return False
        return True

    def _verify(self, nickname: str, cm) -> str:
        """无消耗地校验凭据，并结合服务端额度缓存判断可用性。"""
        try:
            headers = cm.get_headers()
            if not headers.get("Authorization") or not headers.get("X-User-Id"):
                return "exhausted"
            automation = state.account_automation
            if automation is not None:
                quota = automation.get_state(nickname).get("quota") or {}
                if not quota.get("unlimited") and quota.get("remaining") is not None:
                    return "active" if quota["remaining"] > 0 else "exhausted"
            return "active"
        except Exception:
            return "exhausted"

    def pick(self, api_key_entry: dict):
        account_ids = api_key_entry.get("account_ids", [])
        if not account_ids:
            return None
        strategy = api_key_entry.get("strategy", "round-robin")
        now = time.time()

        # 构建可用账号列表（过滤掉已验证不可用的、429 冷却中的）
        candidates = []
        for aid in account_ids:
            cm = self._store.get_cm(aid)
            if not cm:
                continue
            # 检查缓存
            v = self._verified.get(aid)
            if v and v["status"] == "exhausted" and (now - v["checked_at"]) < self.VERIFY_TTL:
                continue  # 上次验证不可用，且在 TTL 内，跳过
            if self._in_cooldown(aid, now):
                continue  # 上游 429 限流冷却中，跳过
            candidates.append((aid, cm))

        if not candidates:
            # 所有账号都不可用，尝试重新验证最早过期的
            self._verified = {}
            for aid in account_ids:
                cm = self._store.get_cm(aid)
                if cm:
                    candidates.append((aid, cm))

        if not candidates:
            return None

        # 选账号
        with self._lock:
            key = api_key_entry["key"]
            if strategy == "round-robin":
                idx = self._index.get(key, 0) % len(candidates)
                self._index[key] = idx + 1
                nickname, cm = candidates[idx]
            elif strategy == "queue":
                idx = self._index.get(key, 0)
                if idx >= len(candidates):
                    idx = 0
                self._index[key] = idx
                nickname, cm = candidates[idx]
                # queue 模式下，只有当前账号不可用时才推进
                v = self._verified.get(nickname)
                if v and v["status"] == "exhausted":
                    self._index[key] = idx + 1
            elif strategy == "quota":
                # 优先使用服务端实时额度；尚未同步时再回退到旧的本地额度。
                scored = []
                for aid, c in candidates:
                    info = self._store.get_info(aid)
                    remaining = float("inf")
                    automation = state.account_automation
                    quota = automation.get_state(aid).get("quota") if automation else None
                    if quota and not quota.get("unlimited") and quota.get("remaining") is not None:
                        remaining = quota["remaining"]
                    elif info and info.get("initial_credit") is not None:
                        remaining = info["initial_credit"] - info.get("total_credits_consumed", 0)
                    scored.append((remaining, aid, c))
                scored.sort(key=lambda x: -x[0])
                nickname, cm = scored[0][1], scored[0][2]
            else:
                nickname, cm = candidates[0]

        # 验证选中的账号（如果缓存过期）
        v = self._verified.get(nickname)
        if not v or (now - v["checked_at"]) > self.VERIFY_TTL:
            status = self._verify(nickname, cm)
            self._verified[nickname] = {"status": status, "checked_at": now}
            if status == "exhausted":
                _log(f"[rotator] {nickname} 验证失败（可能额度耗尽），尝试下一个")
                # 递归重试（排除当前账号）
                remaining = [a for a in candidates if a[0] != nickname]
                if remaining:
                    # 构建临时 entry 只包含剩余账号
                    temp_entry = {**api_key_entry, "account_ids": [a[0] for a in remaining]}
                    return self.pick(temp_entry)

        return nickname, cm
