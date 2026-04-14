#!/usr/bin/env python3
"""JARVIS Runbook — Automated incident response playbooks"""

import redis
import json
import subprocess
import time
from datetime import datetime

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:runbook"

# Runbook definitions: name → steps
RUNBOOKS = {
    "gpu_thermal_warning": {
        "description": "GPU temperature > 78°C — reduce load",
        "steps": [
            ("check_temps", lambda: {f"gpu{i}": r.get(f"jarvis:gpu:{i}:temp") for i in range(5)}),
            ("set_power_limit", lambda: subprocess.run(["nvidia-smi", "-pl", "250"], capture_output=True).returncode),
            ("defer_gpu_tasks", lambda: r.setex("jarvis:scheduler:defer:gpu", 300, "1") or "deferred"),
            ("notify", lambda: r.publish("jarvis:alerts", json.dumps({"type": "gpu_thermal_warning", "severity": "warning", "data": {}}))),
        ],
    },
    "llm_backend_down": {
        "description": "LLM backend unreachable — failover",
        "steps": [
            ("check_backends", lambda: {k: r.hget(f"jarvis:mesh:{k}", "status") for k in ["llm-m2", "llm-ol1"]}),
            ("reset_circuit_breakers", lambda: [r.delete(f"jarvis:cb:{b}") for b in ["m1", "m2", "ol1"]]),
            ("update_routing", lambda: r.set("jarvis:llm:fallback", "ol1")),
            ("notify", lambda: r.lpush("jarvis:event_log", json.dumps({"type": "llm_failover", "severity": "warning", "ts": datetime.now().isoformat()}))),
        ],
    },
    "ram_critical": {
        "description": "RAM < 2GB free — emergency cleanup",
        "steps": [
            ("drop_caches", lambda: subprocess.run(["bash", "-c", "sync && echo 3 > /proc/sys/vm/drop_caches"], capture_output=True).returncode),
            ("clear_redis_temp", lambda: sum(1 for k in r.scan_iter("jarvis:temp:*") if r.delete(k))),
            ("kill_bg_tasks", lambda: r.setex("jarvis:scheduler:defer:background", 600, "1") or "deferred"),
        ],
    },
    "service_restart": {
        "description": "Restart a failed JARVIS service",
        "steps": [
            ("check_services", lambda: subprocess.run(["systemctl", "is-active", "jarvis-api", "jarvis-dashboard"], capture_output=True, text=True).stdout.strip()),
            ("restart_failed", lambda: subprocess.run(["sudo", "systemctl", "restart", "jarvis-api"], capture_output=True).returncode),
        ],
    },
    "full_health_check": {
        "description": "Complete system health verification",
        "steps": [
            ("score", lambda: json.loads(r.get("jarvis:score") or "{}")),
            ("canary", lambda: json.loads(r.get("jarvis:canary:last") or "{}")),
            ("mesh", lambda: json.loads(r.get("jarvis:mesh:summary") or "{}")),
        ],
    },
}


def run_runbook(name: str, dry_run: bool = False) -> dict:
    if name not in RUNBOOKS:
        return {"error": f"unknown runbook: {name}", "available": list(RUNBOOKS.keys())}
    rb = RUNBOOKS[name]
    results = {}
    t0 = time.time()
    for step_name, step_fn in rb["steps"]:
        try:
            if not dry_run:
                result = step_fn()
                results[step_name] = {"ok": True, "result": str(result)[:100]}
            else:
                results[step_name] = {"ok": True, "result": "dry_run_skipped"}
        except Exception as e:
            results[step_name] = {"ok": False, "error": str(e)[:60]}

    duration_ms = round((time.time() - t0) * 1000)
    ok_count = sum(1 for v in results.values() if v["ok"])
    summary = {
        "runbook": name,
        "description": rb["description"],
        "ts": datetime.now().isoformat()[:19],
        "steps": len(rb["steps"]),
        "ok": ok_count,
        "duration_ms": duration_ms,
        "dry_run": dry_run,
        "results": results,
    }
    r.setex(f"{PREFIX}:last:{name}", 3600, json.dumps(summary))
    return summary


def list_runbooks() -> dict:
    return {name: rb["description"] for name, rb in RUNBOOKS.items()}


if __name__ == "__main__":
    import sys
    name = sys.argv[1] if len(sys.argv) > 1 else "full_health_check"
    dry = "--dry" in sys.argv
    print(f"Running runbook: {name} {'(dry)' if dry else ''}")
    res = run_runbook(name, dry_run=dry)
    print(f"Result: {res['ok']}/{res['steps']} steps ok in {res['duration_ms']}ms")
    for step, r_info in res["results"].items():
        icon = "✅" if r_info["ok"] else "❌"
        print(f"  {icon} {step}: {r_info.get('result', r_info.get('error', ''))[:80]}")
