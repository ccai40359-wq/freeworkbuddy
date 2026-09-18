"""日志工具：追加写一行带时间戳的日志到 config.CONFIG["log_path"]。"""
from __future__ import annotations

import threading
import time

from . import config

_LOG_LOCK = threading.Lock()


def log(msg: str):
    path = config.CONFIG.get("log_path")
    if not path:
        return
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    try:
        with _LOG_LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
    except OSError:
        pass
