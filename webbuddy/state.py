"""全局运行时状态（替代原先 globals().get() 的隐式耦合）。

stores / rotator / automation 在 main() 里初始化后赋给本模块，
其它模块通过 `from . import state` 后访问 state.rotator 等属性。
"""
from __future__ import annotations

account_store = None
api_key_store = None
usage_store = None
rotator = None
account_automation = None
