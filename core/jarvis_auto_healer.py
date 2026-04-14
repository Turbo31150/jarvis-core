#!/usr/bin/env python3
"""JARVIS Auto Healer — Automatic remediation based on detected issues"""

import redis
import json
import subprocess
import time
from datetime import datetime

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:healer"

HEAL_COOLDOWN = 300  # 5 min between same heal action


def _cooldown_ok(action: str) -> bool:
    key = f"{PREFIX}:cooldown:{action}"
    if r.get(key):
        return False
    r.setex(key, HEAL_COOLDOWN, "1")
    return True


def heal_circuit_breakers() -> dict:
    if not _cooldown_ok("reset_cb"):
        return {"action": "reset_cb", "skipped": "cooldown"}
    reset_count = 0
    for key in list(r.scan_iter("jarvis:cb:*")):
        state = r.hget(key, "state")
        if state == "open":
            r.delete(key)
            reset_count += 1
    return {"action": "reset_cb", "reset": reset_count}


def heal_dead_consumers() -> dict:
    if not _cooldown_ok("dead_consumers"):
        return {"action": "dead_consumers", "skipped": "cooldown"}
    cleaned = 0
    try:
        groups = r.xinfo_groups("jarvis:stream")
        for g in groups:
            gname = g["name"]
            consumers = r.xinfo_consumers("jarvis:stream", gname)
            for c in consumers:
                if c.get("idle", 0) > 300000:  # 5 min idle
                    r.xgroup_delconsumer("jarvis:stream", gname, c["name"])
                    cleaned += 1
    except Exception:
        pass
    return {"action": "dead_consumers", "cleaned": cleaned}


def heal_redis_memory() -> dict:
    if not _cooldown_ok("redis_memory"):
        return {"action": "redis_memory", "skipped": "cooldown"}
    info = r.info("memory")
    used_mb = info.get("used_memory", 0) / 1024 / 1024
    if used_mb > 1024:  # > 1GB
        deleted = 0
        for pattern in ["jarvis:temp:*", "jarvis:bench:*"]:
            for key in r.scan_iter(pattern):
                if not r.ttl(key):
                    r.delete(key)
                    deleted += 1
        return {"action": "redis_memory", "used_mb": round(used_mb, 1), "deleted": deleted}
    return {"action": "redis_memory", "used_mb": round(used_mb, 1), "ok": True}


def heal_stale_locks() -> dict:
    if not _cooldown_ok("stale_locks"):
        return {"action": "stale_locks", "skipped": "cooldown"}
    cleaned = 0
    for key in r.scan_iter("jarvis:lock:*"):
        ttl = r.ttl(key)
        if ttl == -1:  # no expiry = stale
            r.delete(key)
            cleaned += 1
    return {"action": "stale_locks", "cleaned": cleaned}


def heal_api_gateway() -> dict:
    if not _cooldown_ok("api_gateway"):
        return {"action": "api_gateway", "skipped": "cooldown"}
    try:
        import requests
        resp = requests.get("http://127.0.0.1:8767/health", timeout=3)
        if resp.status_code == 200:
            return {"action": "api_gateway", "ok": True}
    except Exception:
        pass
    # API down — restart
    result = subprocess.run(["sudo", "systemctl", "restart", "jarvis-api"], capture_output=True)
    return {"action": "api_gateway", "restarted": True, "rc": result.returncode}


HEALERS = [
    heal_circuit_breakers,
    heal_dead_consumers,
    heal_redis_memory,
    heal_stale_locks,
]


def run_full_heal(include_services: bool = False) -> dict:
    healers = HEALERS + ([heal_api_gateway] if include_services else [])
    results = []
    for healer in healers:
        try:
            res = healer()
            results.append(res)
        except Exception as e:
            results.append({"action": healer.__name__, "error": str(e)[:60]})

    summary = {
        "ts": datetime.now().isoformat()[:19],
        "healers_run": len(results),
        "actions": results,
    }
    r.setex(f"{PREFIX}:last", 600, json.dumps(summary))
    return summary


if __name__ == "__main__":
    res = run_full_heal()
    print(f"Healer ran {res['healers_run']} actions:")
    for a in res["actions"]:
        skipped = "skipped" in a
        icon = "⏭️" if skipped else "✅"
        print(f"  {icon} {a['action']}: {a}")
