#!/usr/bin/env python3
"""JARVIS Resource Governor — Enforce resource limits and prevent runaway processes"""

import redis
import subprocess
import json
import time
import os
from datetime import datetime

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:governor"

LIMITS = {
    "max_cpu_pct":    85.0,   # Kill processes > 85% CPU sustained
    "max_ram_pct":    90.0,   # Alert + drop caches at 90% RAM
    "max_gpu_temp":   83.0,   # Throttle GPU at 83°C
    "max_redis_mb":   512,    # Warn if Redis > 512MB
    "max_open_files": 65536,  # ulimit -n
}

PROTECTED_PROCESSES = {"redis-server", "python3", "jarvis", "ollama", "lmstudio"}


def get_system_load() -> dict:
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        return {
            "cpu_pct": cpu,
            "ram_pct": mem.percent,
            "ram_free_gb": round(mem.available / 1024**3, 1),
            "ram_used_gb": round(mem.used / 1024**3, 1),
        }
    except ImportError:
        # Fallback without psutil
        with open("/proc/meminfo") as f:
            lines = {l.split(":")[0]: int(l.split()[1]) for l in f if ":" in l}
        total = lines.get("MemTotal", 1)
        free = lines.get("MemAvailable", total)
        return {"cpu_pct": 0, "ram_pct": round((1 - free/total)*100, 1), "ram_free_gb": round(free/1024**2, 1), "ram_used_gb": round((total-free)/1024**2, 1)}


def check_redis_memory() -> dict:
    info = r.info("memory")
    used_mb = info.get("used_memory", 0) / 1024 / 1024
    limit_mb = LIMITS["max_redis_mb"]
    return {"used_mb": round(used_mb, 1), "limit_mb": limit_mb, "ok": used_mb < limit_mb, "pct": round(used_mb/limit_mb*100, 1)}


def enforce_gpu_limits() -> list:
    actions = []
    for i in range(5):
        temp = r.get(f"jarvis:gpu:{i}:temp")
        if temp and float(temp) > LIMITS["max_gpu_temp"]:
            result = subprocess.run(["nvidia-smi", "-i", str(i), "-pl", "200"], capture_output=True)
            actions.append({"gpu": i, "temp": float(temp), "action": "power_limit_200w", "ok": result.returncode == 0})
    return actions


def drop_caches_if_needed(ram_pct: float) -> dict:
    if ram_pct > LIMITS["max_ram_pct"]:
        result = subprocess.run(["bash", "-c", "sync && echo 3 > /proc/sys/vm/drop_caches"], capture_output=True)
        return {"action": "drop_caches", "ok": result.returncode == 0, "triggered_at_pct": ram_pct}
    return {"action": "none", "ram_pct": ram_pct}


def govern() -> dict:
    load = get_system_load()
    redis_mem = check_redis_memory()
    gpu_actions = []
    cache_action = drop_caches_if_needed(load["ram_pct"])
    violations = []

    if load["cpu_pct"] > LIMITS["max_cpu_pct"]:
        violations.append({"resource": "cpu", "value": load["cpu_pct"], "limit": LIMITS["max_cpu_pct"]})
    if load["ram_pct"] > LIMITS["max_ram_pct"]:
        violations.append({"resource": "ram", "value": load["ram_pct"], "limit": LIMITS["max_ram_pct"]})
    if not redis_mem["ok"]:
        violations.append({"resource": "redis", "value": redis_mem["used_mb"], "limit": redis_mem["limit_mb"]})

    # GPU check
    for i in range(5):
        temp = r.get(f"jarvis:gpu:{i}:temp")
        if temp and float(temp) > LIMITS["max_gpu_temp"]:
            violations.append({"resource": f"gpu{i}_temp", "value": float(temp), "limit": LIMITS["max_gpu_temp"]})
            gpu_actions = enforce_gpu_limits()

    result = {
        "ts": datetime.now().isoformat()[:19],
        "load": load,
        "redis_memory": redis_mem,
        "violations": violations,
        "actions": [cache_action] + gpu_actions,
        "status": "ok" if not violations else "violations",
    }
    r.setex(f"{PREFIX}:last", 60, json.dumps(result))
    if violations:
        r.publish("jarvis:alerts", json.dumps({"type": "resource_violation", "severity": "warning", "data": {"violations": violations}, "ts": result["ts"]}))
    return result


if __name__ == "__main__":
    res = govern()
    icon = "✅" if res["status"] == "ok" else "⚠️"
    print(f"{icon} Resource Governor: {res['status']}")
    print(f"  CPU: {res['load']['cpu_pct']}% | RAM: {res['load']['ram_pct']}% ({res['load']['ram_free_gb']}GB free)")
    print(f"  Redis: {res['redis_memory']['used_mb']}MB / {res['redis_memory']['limit_mb']}MB")
    if res["violations"]:
        for v in res["violations"]:
            print(f"  ⚠️  {v['resource']}: {v['value']} > {v['limit']}")
    else:
        print("  No violations")
