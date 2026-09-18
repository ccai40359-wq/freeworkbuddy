"""账号自动化服务：每 N 分钟同步服务端额度，并在活动开放时自动签到。"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime

import httpx

from converter import BACKEND, CredentialManager

from .logging_utils import log as _log
from .models import fetch_official_models


class AccountAutomationService:
    """同步服务端额度，并在活动开放时自动完成每日签到。"""

    STATUS_PATH = "/v2/billing/meter/checkin-activity-status"
    CLAIM_PATH = "/v2/billing/meter/daily-checkin"
    PERSONAL_USAGE_PATH = "/v2/billing/meter/get-user-resource"
    ENTERPRISE_USAGE_PATH = "/v2/billing/meter/get-enterprise-user-usage"

    def __init__(self, store):
        self._store = store
        self._states: dict[str, dict] = {}
        self._state_lock = threading.RLock()
        self._account_locks: dict[str, threading.Lock] = {}
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        try:
            configured_interval = int(
                os.environ.get("WEBBUDDY_ACCOUNT_SYNC_INTERVAL", "300")
            )
        except ValueError:
            configured_interval = 300
        self.interval = max(60, configured_interval)

    @staticmethod
    def _default_state() -> dict:
        return {
            "sync_status": "pending",
            "sync_error": None,
            "synced_at": 0,
            "checkin": None,
            "quota": None,
        }

    def get_state(self, nickname: str) -> dict:
        with self._state_lock:
            state = self._states.get(nickname, self._default_state())
            return json.loads(json.dumps(state, ensure_ascii=False))

    def remove_state(self, nickname: str):
        with self._state_lock:
            self._states.pop(nickname, None)
            self._account_locks.pop(nickname, None)

    def _update_state(self, nickname: str, **changes) -> dict:
        with self._state_lock:
            state = {**self._default_state(), **self._states.get(nickname, {})}
            state.update(changes)
            self._states[nickname] = state
            return json.loads(json.dumps(state, ensure_ascii=False))

    def _account_lock(self, nickname: str) -> threading.Lock:
        with self._state_lock:
            return self._account_locks.setdefault(nickname, threading.Lock())

    @staticmethod
    def _number(value, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _response_payload(response: httpx.Response) -> dict:
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(f"服务端返回了无效数据（HTTP {response.status_code}）") from exc
        if response.status_code != 200:
            message = payload.get("msg") or payload.get("message") or f"HTTP {response.status_code}"
            raise RuntimeError(str(message))
        return payload

    @staticmethod
    def _api_data(payload: dict) -> dict:
        if payload.get("code") != 0:
            raise RuntimeError(str(payload.get("msg") or "服务端请求失败"))
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("服务端响应缺少 data")
        return data

    @staticmethod
    def _checkin_view(data: dict, claim_message: str | None = None) -> dict:
        active = bool(data.get("active"))
        checked = bool(data.get("today_checked_in"))
        state = "checked" if checked else ("pending" if active else "unavailable")
        return {
            "state": state,
            "active": active,
            "today_checked_in": checked,
            "today_credit": data.get("today_credit", 0) or 0,
            "daily_credit": data.get("daily_credit", 0) or 0,
            "streak_days": data.get("streak_days", 0) or 0,
            "theme_name": data.get("theme_name") or "每日签到",
            "season": data.get("season"),
            "activity_end_time": data.get("end_time"),
            "claim_message": claim_message,
        }

    def _sync_checkin(
        self, client: httpx.Client, headers: dict, auto_claim: bool
    ) -> dict:
        status_response = client.post(
            f"{BACKEND}{self.STATUS_PATH}", headers=headers, json={}
        )
        status_data = self._api_data(self._response_payload(status_response))
        checkin = self._checkin_view(status_data)
        if not auto_claim or not checkin["active"] or checkin["today_checked_in"]:
            return checkin

        claim_response = client.post(
            f"{BACKEND}{self.CLAIM_PATH}", headers=headers, json={}
        )
        claim_payload = self._response_payload(claim_response)
        claim_ok = claim_payload.get("code") == 0
        claim_message = str(claim_payload.get("msg") or ("签到成功" if claim_ok else "签到失败"))

        # 无论领取接口返回什么，都重新查询一次，以服务端最终状态为准。
        verify_response = client.post(
            f"{BACKEND}{self.STATUS_PATH}", headers=headers, json={}
        )
        verify_data = self._api_data(self._response_payload(verify_response))
        checkin = self._checkin_view(verify_data, claim_message)
        if not checkin["today_checked_in"]:
            raise RuntimeError(claim_message)
        return checkin

    def _personal_quota(self, client: httpx.Client, headers: dict) -> dict:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        body = {
            "PageNumber": 1,
            "PageSize": 100,
            "ProductCode": "p_tcaca",
            "Status": [0, 3],
            "PackageStartTimeRangeBegin": "2024-12-01 21:25:00",
            "PackageStartTimeRangeEnd": now,
        }
        response = client.post(
            f"{BACKEND}{self.PERSONAL_USAGE_PATH}", headers=headers, json=body
        )
        data = self._api_data(self._response_payload(response))
        response_data = data.get("Response") or {}
        resource_data = response_data.get("Data") or {}
        resources = resource_data.get("Accounts") or []

        packages = []
        total = remaining = used = 0.0
        for resource in resources:
            if not isinstance(resource, dict):
                continue
            package_total = self._number(
                resource.get("CycleCapacitySizePrecise"),
                self._number(resource.get("CapacitySizePrecise")),
            )
            package_remaining = self._number(
                resource.get("CycleCapacityRemainPrecise"),
                self._number(resource.get("CapacityRemainPrecise")),
            )
            package_used = self._number(
                resource.get("CycleCapacityUsedPrecise"),
                max(0.0, package_total - package_remaining),
            )
            total += package_total
            remaining += package_remaining
            used += package_used
            packages.append({
                "name": resource.get("PackageName") or resource.get("PackageCode") or "积分包",
                "code": resource.get("PackageCode"),
                "total": round(package_total, 4),
                "remaining": round(package_remaining, 4),
                "used": round(package_used, 4),
                "expires_at": resource.get("CycleEndTime") or resource.get("DeductionEndTime"),
            })
        return {
            "remaining": round(remaining, 4),
            "total": round(total, 4),
            "used": round(used, 4),
            "unit": "credits",
            "unlimited": False,
            "packages": packages,
        }

    def _enterprise_quota(self, client: httpx.Client, headers: dict) -> dict:
        response = client.post(
            f"{BACKEND}{self.ENTERPRISE_USAGE_PATH}", headers=headers, json={}
        )
        data = self._api_data(self._response_payload(response))
        usage = data.get("data") if isinstance(data.get("data"), dict) else data
        limit = self._number(usage.get("limitNum"))
        used = self._number(usage.get("credit"))
        unlimited = limit == -1
        return {
            "remaining": None if unlimited else round(max(0.0, limit - used), 4),
            "total": None if unlimited else round(limit, 4),
            "used": round(used, 4),
            "unit": "credits",
            "unlimited": unlimited,
            "packages": [],
        }

    @staticmethod
    def _account_metadata(cm: CredentialManager) -> dict:
        try:
            with open(cm.path, "r", encoding="utf-8") as f:
                return (json.load(f).get("account") or {})
        except (OSError, ValueError, TypeError):
            return {}

    def sync_account(self, nickname: str, auto_claim: bool = True) -> dict:
        cm = self._store.get_cm(nickname)
        if cm is None:
            raise KeyError(nickname)
        account_lock = self._account_lock(nickname)
        if not account_lock.acquire(blocking=False):
            return self.get_state(nickname)

        previous = self.get_state(nickname)
        self._update_state(nickname, sync_status="syncing", sync_error=None)
        checkin = previous.get("checkin")
        quota = previous.get("quota")
        errors = []
        try:
            headers = cm.get_headers()
            metadata = self._account_metadata(cm)
            with httpx.Client(timeout=30) as client:
                try:
                    checkin = self._sync_checkin(client, headers, auto_claim)
                except Exception as exc:
                    errors.append(f"签到：{exc}")
                try:
                    if metadata.get("enterpriseId"):
                        quota = self._enterprise_quota(client, headers)
                    else:
                        quota = self._personal_quota(client, headers)
                except Exception as exc:
                    errors.append(f"额度：{exc}")

            any_success = checkin is not None or quota is not None
            sync_status = "ok" if not errors else ("partial" if any_success else "error")
            state = self._update_state(
                nickname,
                sync_status=sync_status,
                sync_error="；".join(errors) if errors else None,
                synced_at=time.time(),
                checkin=checkin,
                quota=quota,
            )
            self._store.set_health_status(nickname, "active" if any_success else "error")
            return state
        except Exception as exc:
            self._store.set_health_status(nickname, "error")
            return self._update_state(
                nickname,
                sync_status="error",
                sync_error=str(exc),
                synced_at=time.time(),
                checkin=checkin,
                quota=quota,
            )
        finally:
            account_lock.release()

    def sync_all(self, auto_claim: bool = True) -> dict:
        results = {}
        for nickname in self._store.list_nicknames():
            try:
                results[nickname] = self.sync_account(nickname, auto_claim=auto_claim)
            except Exception as exc:
                results[nickname] = self._update_state(
                    nickname,
                    sync_status="error",
                    sync_error=str(exc),
                    synced_at=time.time(),
                )
        return results

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()

        def _loop():
            while not self._stop_event.is_set():
                try:
                    self.sync_all(auto_claim=True)
                except Exception as exc:
                    _log(f"[account-sync] {exc}")
                try:
                    fetch_official_models(force=True)
                except Exception as exc:
                    _log(f"[models-sync] {exc}")
                self._stop_event.wait(self.interval)

        self._thread = threading.Thread(
            target=_loop, name="webbuddy-account-sync", daemon=True
        )
        self._thread.start()
