#!/usr/bin/env python3
"""JARVIS Plugin Loader — Dynamic plugin loading and lifecycle management"""

import redis
import importlib
import importlib.util
import sys
import os
import json
from pathlib import Path
from datetime import datetime

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:plugins"
PLUGIN_DIRS = [
    Path("/home/turbo/jarvis/plugins"),
    Path("/home/turbo/IA/Core/jarvis/plugins"),
]

CORE_PLUGINS = {
    "circuit_breaker":  "jarvis_circuit_breaker",
    "rate_limiter":     "jarvis_rate_limiter",
    "llm_router":       "jarvis_llm_router",
    "task_queue":       "jarvis_task_queue",
    "feature_flags":    "jarvis_feature_flags",
    "sla_tracker":      "jarvis_sla_tracker",
    "service_mesh":     "jarvis_service_mesh",
    "health_aggregator":"jarvis_health_aggregator",
    "config_manager":   "jarvis_config_manager",
    "notification_router": "jarvis_notification_router",
}

_loaded = {}


def load_plugin(name: str, module_name: str = None) -> dict:
    mod_name = module_name or f"jarvis_{name}"
    try:
        sys.path.insert(0, "/home/turbo/jarvis/core")
        mod = importlib.import_module(mod_name)
        _loaded[name] = {"module": mod, "name": mod_name, "loaded_at": datetime.now().isoformat()[:19]}
        r.hset(f"{PREFIX}:status", name, "loaded")
        r.hset(f"{PREFIX}:info:{name}", mapping={"module": mod_name, "loaded_at": datetime.now().isoformat()[:19]})
        return {"name": name, "status": "loaded", "module": mod_name}
    except Exception as e:
        r.hset(f"{PREFIX}:status", name, f"error: {str(e)[:40]}")
        return {"name": name, "status": "error", "error": str(e)[:60]}


def unload_plugin(name: str) -> bool:
    if name in _loaded:
        mod_name = _loaded[name]["name"]
        if mod_name in sys.modules:
            del sys.modules[mod_name]
        del _loaded[name]
        r.hset(f"{PREFIX}:status", name, "unloaded")
        return True
    return False


def reload_plugin(name: str) -> dict:
    unload_plugin(name)
    mod_name = CORE_PLUGINS.get(name, f"jarvis_{name}")
    return load_plugin(name, mod_name)


def load_all_core() -> dict:
    results = {}
    for name, mod_name in CORE_PLUGINS.items():
        results[name] = load_plugin(name, mod_name)
    loaded_count = sum(1 for v in results.values() if v["status"] == "loaded")
    r.setex(f"{PREFIX}:last_load", 3600, json.dumps({"ts": datetime.now().isoformat()[:19], "loaded": loaded_count, "total": len(CORE_PLUGINS)}))
    return results


def get_plugin(name: str):
    """Get loaded plugin module"""
    return _loaded.get(name, {}).get("module")


def status() -> dict:
    return {
        "loaded_count": len(_loaded),
        "loaded": list(_loaded.keys()),
        "registry": r.hgetall(f"{PREFIX}:status"),
    }


if __name__ == "__main__":
    print(f"Loading {len(CORE_PLUGINS)} core plugins...")
    results = load_all_core()
    loaded = sum(1 for v in results.values() if v["status"] == "loaded")
    errors = sum(1 for v in results.values() if v["status"] == "error")
    print(f"  {loaded} loaded, {errors} errors")
    for name, res in results.items():
        icon = "✅" if res["status"] == "loaded" else "❌"
        print(f"  {icon} {name}: {res['status']}")
