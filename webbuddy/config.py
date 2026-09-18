"""运行时配置与数据目录路径。

数据目录相关路径在 configure_data_dir() 中统一定义/重绑，
其它模块通过 `from . import config` 后访问 config.X 属性
（不要 from config import X，否则重绑后拿到旧值）。
"""
from __future__ import annotations

import os
from pathlib import Path

# 数据目录（可用 WEBBUDDY_DATA_DIR 或 --data-dir 覆盖）
DATA_DIR = Path(os.environ.get("WEBBUDDY_DATA_DIR", "./data")).expanduser().resolve()
AUTHS_DIR = DATA_DIR / "auths"
KEYS_FILE = DATA_DIR / "api_keys.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
USAGE_FILE = DATA_DIR / "usage.jsonl"
ACCOUNTS_DB = DATA_DIR / "accounts.json"

# 运行时配置
CONFIG = {
    "desensitize": True,
    "log_path": None,
    "secure_cookie": False,
}


def configure_data_dir(path):
    """重绑数据目录及所有派生路径（修复 USAGE_FILE 不随 --data-dir 重绑的 bug）。"""
    global DATA_DIR, AUTHS_DIR, KEYS_FILE, SETTINGS_FILE, USAGE_FILE, ACCOUNTS_DB
    DATA_DIR = Path(path).expanduser().resolve()
    AUTHS_DIR = DATA_DIR / "auths"
    KEYS_FILE = DATA_DIR / "api_keys.json"
    SETTINGS_FILE = DATA_DIR / "settings.json"
    USAGE_FILE = DATA_DIR / "usage.jsonl"
    ACCOUNTS_DB = DATA_DIR / "accounts.json"


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
