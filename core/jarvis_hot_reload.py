#!/usr/bin/env python3
"""JARVIS Hot Reload — Watch for file changes and reload modules without restart"""

import redis
import importlib
import sys
import os
import time
import hashlib
from pathlib import Path
from datetime import datetime

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:hot_reload"
CORE_DIR = Path("/home/turbo/jarvis/core")


def file_hash(filepath: str) -> str:
    try:
        with open(filepath, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()[:8]
    except Exception:
        return ""


def get_stored_hash(module: str) -> str:
    return r.hget(f"{PREFIX}:hashes", module) or ""


def store_hash(module: str, h: str):
    r.hset(f"{PREFIX}:hashes", module, h)


def reload_module(module_name: str) -> dict:
    t0 = time.perf_counter()
    try:
        if module_name in sys.modules:
            mod = importlib.reload(sys.modules[module_name])
        else:
            sys.path.insert(0, str(CORE_DIR))
            mod = importlib.import_module(module_name)
        ms = round((time.perf_counter() - t0) * 1000)
        r.hincrby(f"{PREFIX}:stats", "reloads", 1)
        r.lpush(f"{PREFIX}:log", f"{datetime.now().isoformat()[:19]} RELOAD {module_name} {ms}ms")
        r.ltrim(f"{PREFIX}:log", 0, 99)
        return {"ok": True, "module": module_name, "ms": ms}
    except Exception as e:
        r.hincrby(f"{PREFIX}:stats", "errors", 1)
        return {"ok": False, "module": module_name, "error": str(e)[:60]}


def check_and_reload_changed() -> list:
    reloaded = []
    for filepath in CORE_DIR.glob("jarvis_*.py"):
        module_name = filepath.stem
        current_hash = file_hash(str(filepath))
        stored = get_stored_hash(module_name)
        if stored and current_hash != stored:
            result = reload_module(module_name)
            store_hash(module_name, current_hash)
            reloaded.append(result)
            r.publish("jarvis:events", __import__("json").dumps({
                "type": "module_reloaded",
                "severity": "info",
                "data": {"module": module_name, "ok": result["ok"]},
                "ts": datetime.now().isoformat(),
            }))
        elif not stored:
            store_hash(module_name, current_hash)
    return reloaded


def scan_all_hashes() -> int:
    """Initialize hashes for all modules (no reload, just register)"""
    count = 0
    for filepath in CORE_DIR.glob("jarvis_*.py"):
        store_hash(filepath.stem, file_hash(str(filepath)))
        count += 1
    return count


def stats() -> dict:
    data = r.hgetall(f"{PREFIX}:stats")
    recent = r.lrange(f"{PREFIX}:log", 0, 4)
    return {
        "total_reloads": int(data.get("reloads", 0)),
        "errors": int(data.get("errors", 0)),
        "tracked_modules": r.hlen(f"{PREFIX}:hashes"),
        "recent": [l for l in recent],
    }


if __name__ == "__main__":
    import sys
    if "--watch" in sys.argv:
        count = scan_all_hashes()
        print(f"[HotReload] Watching {count} modules for changes...")
        while True:
            changed = check_and_reload_changed()
            if changed:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Reloaded: {[r['module'] for r in changed]}")
            time.sleep(2)
    elif "--scan" in sys.argv:
        count = scan_all_hashes()
        print(f"Scanned {count} modules, hashes stored")
    else:
        count = scan_all_hashes()
        changed = check_and_reload_changed()
        print(f"Tracked: {count} modules | Changed: {len(changed)}")
        print(f"Stats: {stats()}")
