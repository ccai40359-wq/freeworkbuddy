#!/usr/bin/env python3
"""
codebuddy2openai Web GUI — 多账号管理面板 + OpenAI 兼容 API。

用法：
  python webgui.py --port 8788 --data-dir ./data

功能：
  - 拖拽上传 auth.json 文件，自动创建账号
  - 生成 API Key，绑定多个账号
  - 三种账号轮换策略：轮流 / 队列 / 按额度
  - 查看每个账号的额度消耗
  - 启动时自动扫描 ./data/auths/ 目录下的已有文件

代码结构（自 2026-08-04 起拆分为 webbuddy 包）：
  webbuddy/config.py        运行时配置与数据目录路径
  webbuddy/auth.py          管理员认证（密码哈希/会话/限流/中间件）
  webbuddy/stores.py        AccountStore / ApiKeyStore / UsageStore
  webbuddy/rotator.py       账号轮换（round-robin/queue/quota + 429 冷却）
  webbuddy/automation.py    自动签到 + 服务端额度同步
  webbuddy/routes/admin.py  管理面板 API 路由
  webbuddy/routes/openai.py OpenAI 兼容端点 + 429 failover + credit 记账
  webbuddy/static/          前端页面（index.html / login.html）
"""
from __future__ import annotations

import argparse
import os
import sys

import uvicorn
from fastapi import FastAPI

from converter import UpstreamHTTPException, upstream_exception_handler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from webbuddy import config, state
from webbuddy.auth import AuthMiddleware, get_admin_settings
from webbuddy.automation import AccountAutomationService
from webbuddy.rotator import AccountRotator
from webbuddy.routes import admin as admin_routes
from webbuddy.routes import openai as openai_routes
from webbuddy.stores import AccountStore, ApiKeyStore, UsageStore

app = FastAPI(title="CodeBuddy2OpenAI Web GUI", version="2.0")
app.add_exception_handler(UpstreamHTTPException, upstream_exception_handler)
app.add_middleware(AuthMiddleware)
app.include_router(admin_routes.router)
app.include_router(openai_routes.router)


def main():
    ap = argparse.ArgumentParser(description="CodeBuddy Web GUI 多账号管理面板")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument(
        "--data-dir",
        default=os.environ.get("WEBBUDDY_DATA_DIR", "./data"),
        help="数据目录（也可用 WEBBUDDY_DATA_DIR）",
    )
    ap.add_argument("--log", default=None, help="日志文件路径")
    ap.add_argument("--desensitize", action="store_true", help="启用脱敏")
    ap.add_argument(
        "--secure-cookie",
        action="store_true",
        default=config.env_bool("WEBBUDDY_SECURE_COOKIE"),
        help="仅通过 HTTPS 发送管理会话 Cookie",
    )
    args = ap.parse_args()

    config.configure_data_dir(args.data_dir)
    state.account_store = AccountStore()
    state.api_key_store = ApiKeyStore()
    state.usage_store = UsageStore()
    state.rotator = AccountRotator(state.account_store)
    state.account_automation = AccountAutomationService(state.account_store)

    if args.log:
        config.CONFIG["log_path"] = args.log
    config.CONFIG["secure_cookie"] = args.secure_cookie

    settings = get_admin_settings()
    config.CONFIG["desensitize"] = args.desensitize or bool(settings.get("desensitize", True))

    # 自动扫描已有 auth 文件
    config.AUTHS_DIR.mkdir(parents=True, exist_ok=True)
    for f in sorted(config.AUTHS_DIR.glob("*.json")):
        try:
            nickname = f.stem
            state.account_store.add(nickname, str(f))
            sys.stderr.write(f"  [加载] {nickname}\n")
        except Exception as e:
            sys.stderr.write(f"  [跳过] {f.name}: {e}\n")

    sys.stderr.write(f"\n🚀 CodeBuddy Web GUI 启动\n")
    sys.stderr.write(f"   地址: http://{args.host}:{args.port}\n")
    sys.stderr.write(f"   数据: {config.DATA_DIR.resolve()}\n")
    sys.stderr.write(f"   管理员: {settings['admin_username']}\n")
    sys.stderr.write(f"   账号: {len(state.account_store.list())} 个\n")
    sys.stderr.write(f"   Keys: {len(state.api_key_store.list())} 个\n")
    sys.stderr.write(f"   Cookie: {'HTTPS only' if config.CONFIG['secure_cookie'] else 'HTTP/HTTPS'}\n")

    # 启动后立即同步，之后定期检查未签到账号和实时额度。
    state.account_automation.start()
    sys.stderr.write(
        f"   自动签到/额度同步: 每 {state.account_automation.interval // 60} 分钟检查\n"
    )

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
