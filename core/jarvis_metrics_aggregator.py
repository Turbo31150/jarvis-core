#!/usr/bin/env python3
"""JARVIS Metrics Aggregator — Aggregate metrics from all modules into unified view"""

import redis
import json
import time
from datetime import datetime

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:metrics_agg"


def collect_all() -> dict:
    metrics = {}
    ts = datetime.now().isoformat()[:19]

    # Score
    score = json.loads(r.get("jarvis:score") or "{}")
    metrics["score"] = {
        "total": score.get("total", 0),
        "cpu_temp": score.get("cpu_temp", 0),
        "gpu_max_temp": score.get("gpu_max_temp", 0),
        "ram_free_gb": score.get("ram_free_gb", 0),
    }

    # GPUs
    gpu_temps = []
    for i in range(5):
        t = r.get(f"jarvis:gpu:{i}:temp")
        v = r.get(f"jarvis:gpu:{i}:vram_pct")
        if t:
            metrics[f"gpu{i}"] = {"temp": float(t), "vram_pct": float(v or 0)}
            gpu_temps.append(float(t))
    if gpu_temps:
        metrics["gpu_summary"] = {"max_temp": max(gpu_temps), "min_temp": min(gpu_temps), "avg_temp": round(sum(gpu_temps)/len(gpu_temps), 1)}

    # LLM router
    llm_calls = 0
    llm_errors = 0
    for key in r.scan_iter("jarvis:llm_router:*:count"):
        llm_calls += int(r.get(key) or 0)
    for key in r.scan_iter("jarvis:llm_router:*:errors"):
        llm_errors += int(r.get(key) or 0)
    metrics["llm"] = {"total_calls": llm_calls, "errors": llm_errors, "error_rate": round(llm_errors / max(llm_calls, 1) * 100, 2)}

    # Circuit breakers
    cb_open = 0
    for key in r.scan_iter("jarvis:cb:*"):
        if r.hget(key, "state") == "open":
            cb_open += 1
    metrics["circuit_breakers"] = {"open": cb_open}

    # Service mesh
    mesh = json.loads(r.get("jarvis:mesh:summary") or "{}")
    if mesh:
        metrics["services"] = {"up": mesh.get("up", 0), "total": mesh.get("total", 0)}

    # Canary
    canary = json.loads(r.get("jarvis:canary:last") or "{}")
    if canary:
        metrics["canary"] = {"passed": canary.get("passed", 0), "failed": canary.get("failed", 0)}

    # Nodes
    node_status = {}
    for n in ["M1", "M2", "M3", "OL1"]:
        node_status[n] = r.get(f"jarvis:node:{n}:status") or "unknown"
    metrics["nodes"] = node_status

    result = {"ts": ts, "metrics": metrics}
    r.setex(f"{PREFIX}:last", 120, json.dumps(result))
    return result


def diff(prev: dict, curr: dict) -> dict:
    """Compute difference between two snapshots"""
    changes = {}
    for key in set(list(prev.get("metrics", {}).keys()) + list(curr.get("metrics", {}).keys())):
        p = prev.get("metrics", {}).get(key, {})
        c = curr.get("metrics", {}).get(key, {})
        if isinstance(p, dict) and isinstance(c, dict):
            for k in set(list(p.keys()) + list(c.keys())):
                pv, cv = p.get(k), c.get(k)
                if isinstance(pv, (int, float)) and isinstance(cv, (int, float)) and pv != cv:
                    changes[f"{key}.{k}"] = {"prev": pv, "curr": cv, "delta": round(cv - pv, 3)}
    return changes


if __name__ == "__main__":
    res = collect_all()
    print(f"Metrics collected at {res['ts']}:")
    for key, val in res["metrics"].items():
        print(f"  {key}: {val}")
