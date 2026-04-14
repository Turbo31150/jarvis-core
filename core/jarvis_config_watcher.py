#!/usr/bin/env python3
"""JARVIS Config Watcher — Hot-reload config files without service restart"""
import os, time, json, redis, hashlib
from datetime import datetime

r = redis.Redis(decode_responses=True)

WATCHED = [
    "/home/turbo/IA/Core/jarvis/config/secrets.env",
    "/home/turbo/jarvis/config/scheduled-tasks.json",
    "/home/turbo/.config/jarvis/secrets.env",
]

def _hash(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except:
        return ""

def watch_once() -> list:
    changed = []
    for path in WATCHED:
        key = f"jarvis:cfgwatch:{hashlib.md5(path.encode()).hexdigest()[:8]}"
        current = _hash(path)
        prev = r.get(key)
        if prev and prev != current:
            changed.append(path)
            r.publish("jarvis:events", json.dumps({
                "type": "config_changed",
                "data": {"path": path},
                "severity": "info",
                "ts": datetime.now().isoformat()[:19]
            }))
            print(f"[ConfigWatcher] Changed: {path}")
        r.set(key, current)
    return changed

def watch_loop(interval: int = 30):
    print(f"[ConfigWatcher] Watching {len(WATCHED)} files every {interval}s")
    while True:
        watch_once()
        time.sleep(interval)

if __name__ == "__main__":
    changed = watch_once()
    print(f"Initial scan: {len(WATCHED)} files, {len(changed)} changed")
    if changed:
        for p in changed:
            print(f"  → {p}")
