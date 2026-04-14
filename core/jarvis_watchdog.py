#!/usr/bin/env python3
"""JARVIS Watchdog — Monitor and auto-restart critical processes"""

import redis
import subprocess
import time
import json
from datetime import datetime

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:watchdog"

WATCHED_SERVICES = [
    {"name": "jarvis-api",           "restart_cmd": ["sudo", "systemctl", "restart", "jarvis-api"],          "check_url": "http://127.0.0.1:8767/health"},
    {"name": "jarvis-dashboard",     "restart_cmd": ["sudo", "systemctl", "restart", "jarvis-dashboard"],    "check_url": "http://127.0.0.1:8765/"},
    {"name": "jarvis-prometheus",    "restart_cmd": ["sudo", "systemctl", "restart", "jarvis-prometheus"],   "check_url": "http://127.0.0.1:9090/metrics"},
    {"name": "jarvis-hw-monitor",    "restart_cmd": ["sudo", "systemctl", "restart", "jarvis-hw-monitor"],   "check_url": None},
]

MAX_RESTARTS = 3
RESTART_WINDOW = 600  # 10 min


def is_healthy(svc: dict) -> bool:
    # systemctl check
    result = subprocess.run(["systemctl", "is-active", svc["name"]], capture_output=True, text=True)
    if result.stdout.strip() != "active":
        return False
    # HTTP check if configured
    if svc.get("check_url"):
        try:
            import requests
            resp = requests.get(svc["check_url"], timeout=3)
            return resp.status_code < 500
        except Exception:
            return False
    return True


def restart_count(name: str) -> int:
    return int(r.get(f"{PREFIX}:restarts:{name}") or 0)


def record_restart(name: str):
    key = f"{PREFIX}:restarts:{name}"
    r.incr(key)
    r.expire(key, RESTART_WINDOW)


def watch_once() -> dict:
    results = {}
    for svc in WATCHED_SERVICES:
        name = svc["name"]
        healthy = is_healthy(svc)
        if healthy:
            results[name] = {"status": "ok"}
            r.setex(f"{PREFIX}:status:{name}", 120, "ok")
            continue
        # Unhealthy — check restart limit
        restarts = restart_count(name)
        if restarts >= MAX_RESTARTS:
            results[name] = {"status": "failed", "restarts": restarts, "action": "max_restarts_reached"}
            r.setex(f"{PREFIX}:status:{name}", 120, "failed")
            # Notify
            r.publish("jarvis:alerts", json.dumps({
                "type": "service_max_restarts",
                "severity": "critical",
                "data": {"service": name, "restarts": restarts},
                "ts": datetime.now().isoformat(),
            }))
            continue
        # Restart
        result = subprocess.run(svc["restart_cmd"], capture_output=True)
        record_restart(name)
        results[name] = {"status": "restarted", "rc": result.returncode, "restarts": restarts + 1}
        r.setex(f"{PREFIX}:status:{name}", 120, "restarted")

    summary = {
        "ts": datetime.now().isoformat()[:19],
        "ok": sum(1 for v in results.values() if v["status"] == "ok"),
        "restarted": sum(1 for v in results.values() if v["status"] == "restarted"),
        "failed": sum(1 for v in results.values() if v["status"] == "failed"),
        "results": results,
    }
    r.setex(f"{PREFIX}:last", 120, json.dumps(summary))
    return summary


if __name__ == "__main__":
    import sys
    if "--watch" in sys.argv:
        print(f"[Watchdog] Monitoring {len(WATCHED_SERVICES)} services every 30s...")
        while True:
            res = watch_once()
            if res["restarted"] or res["failed"]:
                print(f"[{res['ts']}] restarted={res['restarted']} failed={res['failed']}")
            time.sleep(30)
    else:
        res = watch_once()
        print(f"Watchdog: {res['ok']} ok, {res['restarted']} restarted, {res['failed']} failed")
        for name, status in res["results"].items():
            icon = "✅" if status["status"] == "ok" else ("🔄" if status["status"] == "restarted" else "❌")
            print(f"  {icon} {name}: {status['status']}")
