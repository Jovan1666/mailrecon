#!/usr/bin/env python3
"""确认 task-cmd-bridge 插件被加载、且注册了 pre_gateway_dispatch 回调。"""
import sys
sys.path.insert(0, "/home/hermes/.local/share/uv/tools/hermes-agent/lib/python3.11/site-packages")
from hermes_cli.config import load_config
from hermes_cli.plugins import _get_enabled_plugins, get_plugin_manager
cfg = load_config()
print("config plugins        :", cfg.get("plugins"))
print("enabled set           :", _get_enabled_plugins())
m = get_plugin_manager(); m.discover_and_load()
hit = [(k, lp.enabled, lp.hooks_registered) for k, lp in (m._plugins or {}).items() if "task-cmd" in str(k)]
print("插件加载情况           :", hit or "!! 没加载")
print("pre_gateway_dispatch 回调:", [getattr(c, "__name__", c) for c in m._hooks.get("pre_gateway_dispatch", [])])
