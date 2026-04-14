#!/usr/bin/env python3
"""JARVIS Service Mesh — Service discovery, health routing, load balancing"""

import redis
import time
import json
import requests
from threading import Thread

r = redis.Redis(decode_responses=True)

PREFIX = "jarvis:mesh"

SERVICES = {
    "api-gateway":  {"host": "127.0.0.1", "port": 8767, "path": "/health"},
    "dashboard":    {"host": "127.0.0.1", "port": 8765, "path": "/"},
    "prometheus":   {"host": "127.0.0.1", "port": 9090, "path": "/metrics"},
    "llm-m2":       {"host": "192.168.1.26", "port": 1234, "path": "/health"},
    "llm-ol1":      {"host": "127.0.0.1", "port": 11434, "path": "/"},
    "redis":        {"host": "127.0.0.1", "port": 6379, "path": None},
}

HEALTH_INTERVAL = 30  # seconds


def check_service(name: str, cfg: dict) -> dict:
    t0 = time.perf_counter()
    try:
        if cfg["path"] is None:
            # Redis check
            r.ping()
            latency_ms = round((time.perf_counter() - t0) * 1000)
            return {"name": name, "status": "up", "latency_ms": latency_ms}

        url = f"http://{cfg['host']}:{cfg['port']}{cfg['path']}"
        resp = requests.get(url, timeout=5)
        latency_ms = round((time.perf_counter() - t0) * 1000)
        up = resp.status_code < 500
        return {"name": name, "status": "up" if up else "degraded", "latency_ms": latency_ms, "code": resp.status_code}
    except Exception as e:
        latency_ms = round((time.perf_counter() - t0) * 1000)
        return {"name": name, "status": "down", "latency_ms": latency_ms, "error": str(e)[:60]}


def scan_all() -> dict:
    results = {}
    threads = []

    def _check(name, cfg):
        result = check_service(name, cfg)
        results[name] = result
        key = f"{PREFIX}:{name}"
        r.hset(key, mapping={
            "status": result["status"],
            "latency_ms": result.get("latency_ms", 0),
            "last_check": time.strftime("%H:%M:%S"),
            "error": result.get("error", ""),
        })
        r.expire(key, 120)

    for name, cfg in SERVICES.items():
        t = Thread(target=_check, args=(name, cfg))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()

    up_count = sum(1 for v in results.values() if v["status"] == "up")
    summary = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total": len(SERVICES),
        "up": up_count,
        "down": len(SERVICES) - up_count,
        "services": results,
    }
    r.setex(f"{PREFIX}:summary", 120, json.dumps(summary))
    return summary


def get_best_llm(task_type: str = "default") -> dict:
    """Return the best available LLM endpoint for a task type"""
    candidates = {
        "llm-m2": {"host": "192.168.1.26:1234", "priority": 1},
        "llm-ol1": {"host": "127.0.0.1:11434", "priority": 2},
    }
    for name, meta in candidates.items():
        info = r.hgetall(f"{PREFIX}:{name}")
        if info.get("status") == "up":
            lat = int(info.get("latency_ms", 9999))
            meta["latency_ms"] = lat
            return {"backend": name, **meta}
    return {"backend": "none", "error": "all LLM backends down"}


def topology() -> dict:
    """Current service topology from Redis"""
    result = {}
    for name in SERVICES:
        info = r.hgetall(f"{PREFIX}:{name}")
        result[name] = info if info else {"status": "unknown"}
    return result


def watch():
    """Background health watcher — runs scan every HEALTH_INTERVAL"""
    print(f"[ServiceMesh] Watching {len(SERVICES)} services every {HEALTH_INTERVAL}s")
    while True:
        try:
            summary = scan_all()
            print(f"[{time.strftime('%H:%M:%S')}] {summary['up']}/{summary['total']} up")
        except Exception as e:
            print(f"[ServiceMesh] ERR: {e}")
        time.sleep(HEALTH_INTERVAL)


if __name__ == "__main__":
    import sys
    if "--watch" in sys.argv:
        watch()
    else:
        result = scan_all()
        print(f"Mesh scan: {result['up']}/{result['total']} up")
        for name, info in result["services"].items():
            icon = "✅" if info["status"] == "up" else ("⚠️" if info["status"] == "degraded" else "❌")
            lat = info.get("latency_ms", "?")
            print(f"  {icon} {name}: {info['status']} ({lat}ms)")
