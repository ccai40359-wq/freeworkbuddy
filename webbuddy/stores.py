"""数据持久化：账号、API Key、用量流水。纯 JSON 文件，无数据库。"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path

from converter import CredentialManager

from . import config


def _load_json(path: Path, default=None):
    if not path.exists():
        return default or {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default or {}


def _save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


class AccountStore:
    """持久化账号信息。"""

    def __init__(self):
        self._accounts: dict = {}  # nickname -> {nickname, auth_path, initial_credit, notes}
        self._cms: dict[str, CredentialManager] = {}  # nickname -> CredentialManager
        self._load()

    def _load(self):
        config.ACCOUNTS_DB.parent.mkdir(parents=True, exist_ok=True)
        data = _load_json(config.ACCOUNTS_DB, {})
        self._accounts = data.get("accounts", {})
        # 初始化 CredentialManager
        for nickname, info in list(self._accounts.items()):
            p = Path(info["auth_path"])
            if p.exists():
                try:
                    self._cms[nickname] = CredentialManager(p)
                except Exception:
                    pass

    def _save(self):
        _save_json(config.ACCOUNTS_DB, {"accounts": self._accounts})

    def list_nicknames(self) -> list:
        return list(self._accounts.keys())

    def list(self) -> list:
        """返回账号列表（含额度信息）。"""
        result = []
        for nickname, info in self._accounts.items():
            cm = self._cms.get(nickname)
            cred_info = {}
            if cm:
                try:
                    cred_info = cm.summary()
                except Exception:
                    cred_info = {"error": "无法读取凭据"}
            # 计算已消耗额度
            consumed = info.get("total_credits_consumed", 0)
            initial = info.get("initial_credit")
            result.append({
                "nickname": nickname,
                "auth_file": info["auth_path"],
                "consumed_credits": consumed,
                "initial_credit": initial,
                "remaining": round(initial - consumed, 4) if initial is not None else None,
                "status": "active" if initial is None or consumed < initial else "exhausted",
                "health_status": info.get("health_status", "unknown"),
                "last_checked": info.get("last_checked", 0),
                "credential": cred_info,
            })
        return result

    def add(self, nickname: str, auth_path: str) -> dict:
        """添加一个账号。"""
        p = Path(auth_path)
        if not p.exists():
            raise ValueError(f"auth 文件不存在：{auth_path}")
        cm = CredentialManager(p)
        cm.summary()  # 尝试读取，失败会抛异常
        self._cms[nickname] = cm
        existing = self._accounts.get(nickname, {})
        self._accounts[nickname] = {
            "nickname": nickname,
            "auth_path": auth_path,
            "initial_credit": existing.get("initial_credit"),
            "total_credits_consumed": existing.get("total_credits_consumed", 0),
            "notes": existing.get("notes", ""),
            "health_status": existing.get("health_status", "unknown"),
            "last_checked": existing.get("last_checked", 0),
        }
        self._save()
        return self._accounts[nickname]

    def remove(self, nickname: str):
        self._accounts.pop(nickname, None)
        self._cms.pop(nickname, None)
        self._save()

    def set_initial_credit(self, nickname: str, credits: float):
        if nickname not in self._accounts:
            raise ValueError(f"账号不存在：{nickname}")
        self._accounts[nickname]["initial_credit"] = credits
        self._save()

    def set_health_status(self, nickname: str, status: str):
        if nickname in self._accounts:
            self._accounts[nickname]["health_status"] = status
            self._accounts[nickname]["last_checked"] = time.time()
            self._save()

    def get_health_status(self, nickname: str) -> str:
        return self._accounts.get(nickname, {}).get("health_status", "unknown")

    def record_consumption(self, nickname: str, credits: float):
        """记录额度消耗。"""
        if nickname in self._accounts:
            self._accounts[nickname]["total_credits_consumed"] = (
                self._accounts[nickname].get("total_credits_consumed", 0) + credits)
            self._save()

    def get_cm(self, nickname: str):
        return self._cms.get(nickname)

    def get_info(self, nickname: str):
        return self._accounts.get(nickname)


class ApiKeyStore:
    def __init__(self):
        self._keys: dict = {}
        self._load()

    def _load(self):
        config.KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._keys = _load_json(config.KEYS_FILE, {})

    def _save(self):
        _save_json(config.KEYS_FILE, self._keys)

    def list(self) -> list:
        return list(self._keys.values())

    def create(self, name: str, account_ids: list, strategy: str = "round-robin") -> dict:
        key = "sk-" + secrets.token_hex(24)
        entry = {
            "key": key,
            "name": name,
            "account_ids": account_ids,
            "strategy": strategy,
            "enabled": True,
            "created_at": time.time(),
        }
        self._keys[key] = entry
        self._save()
        return entry

    def delete(self, key: str):
        self._keys.pop(key, None)
        self._save()

    def regenerate(self, key: str):
        entry = self._keys.get(key)
        if not entry:
            return None
        new_key = "sk-" + secrets.token_hex(24)
        entry["key"] = new_key
        self._keys[new_key] = entry
        del self._keys[key]
        self._save()
        return entry

    def get(self, key: str):
        return self._keys.get(key)


class UsageStore:
    """按请求记录积分消耗流水（append-only jsonl），支持按自然日（北京时间）聚合。"""

    def __init__(self):
        self._lock = threading.Lock()
        config.USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)

    def record(self, account: str, credit: float, model: str = "",
               key_entry: dict | None = None, rid: str = ""):
        entry = {
            "ts": time.time(),
            "account": account,
            "model": model or "?",
            "credit": credit,
        }
        if key_entry:
            entry["key_name"] = key_entry.get("name", "")
            entry["key_tail"] = key_entry.get("key", "")[-4:]
        if rid:
            entry["rid"] = rid
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            try:
                with open(config.USAGE_FILE, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass

    @staticmethod
    def _bj_day_start(ts: float | None = None) -> float:
        """北京时间当天 0 点的 Unix 时间戳。"""
        now = ts if ts is not None else time.time()
        return (int(now) + 8 * 3600) // 86400 * 86400 - 8 * 3600

    def query_today(self) -> dict:
        """聚合北京时间今日 0 点以来的用量：总量、按模型、按 key、按账号。"""
        start = self._bj_day_start()
        total = 0.0
        count = 0
        by_model: dict[str, dict] = {}
        by_key: dict[str, dict] = {}
        by_account: dict[str, dict] = {}
        try:
            with open(config.USAGE_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    if e.get("ts", 0) < start:
                        continue
                    credit = float(e.get("credit", 0))
                    total += credit
                    count += 1
                    m = e.get("model") or "?"
                    slot = by_model.setdefault(m, {"model": m, "credit": 0.0, "count": 0})
                    slot["credit"] += credit
                    slot["count"] += 1
                    kn = e.get("key_name") or "(直接调用)"
                    kt = e.get("key_tail") or ""
                    klabel = f"{kn} (…{kt})" if kt else kn
                    slot = by_key.setdefault(klabel, {"key": klabel, "credit": 0.0, "count": 0})
                    slot["credit"] += credit
                    slot["count"] += 1
                    a = e.get("account") or "?"
                    slot = by_account.setdefault(a, {"account": a, "credit": 0.0, "count": 0})
                    slot["credit"] += credit
                    slot["count"] += 1
        except OSError:
            pass
        srt = lambda d: sorted(d.values(), key=lambda x: -x["credit"])
        return {
            "date": time.strftime("%Y-%m-%d", time.gmtime(start + 8 * 3600)),
            "total_credit": round(total, 2),
            "request_count": count,
            "by_model": srt(by_model),
            "by_key": srt(by_key),
            "by_account": srt(by_account),
        }
