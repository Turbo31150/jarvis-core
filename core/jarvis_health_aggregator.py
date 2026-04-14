#!/usr/bin/env python3
"""JARVIS Health Aggregator — Unified health from GPU, nodes, LLMs, services, score"""

import redis
import json
import time
from datetime import datetime

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:health"


def gpu_health() -> dict:
    gpus = {}
    all_ok = True
    for i in range(5):
        temp = r.get(f"jarvis:gpu:{i}:temp")
        vram = r.get(f"jarvis:gpu:{i}:vram_pct")
        if temp is None:
            continue
        t = float(temp)
        status = "ok" if t < 70 else ("warn" if t < 82 else "critical")
        if status != "ok":
            all_ok = False
        gpus[f"gpu{i}"] = {"temp": t, "vram_pct": vram, "status": status}
    return {"status": "ok" if all_ok else "degraded", "gpus": gpus}


def node_health() -> dict:
    nodes = {}
    for n in ["M1", "M2", "M3", "OL1"]:
        s = r.get(f"jarvis:node:{n}:status") or "unknown"
        nodes[n] = s
    up = sum(1 for v in nodes.values() if v == "up")
    return {"status": "ok" if up >= 2 else ("degraded" if up >= 1 else "critical"), "nodes": nodes, "up": up}


def llm_health() -> dict:
    backends = {}
    for key in r.scan_iter("jarvis:llm:m*"):
        d = json.loads(r.get(key) or "{}")
        name = key.replace("jarvis:llm:", "")
        backends[name] = {"ok": d.get("ok", False), "latency_ms": d.get("latency_ms", "?")}
    ok_count = sum(1 for v in backends.values() if v["ok"])
    return {"status": "ok" if ok_count > 0 else "critical", "backends": backends, "available": ok_count}


def service_health() -> dict:
    import subprocess
    svcs = ["jarvis-api", "jarvis-dashboard", "jarvis-prometheus", "jarvis-hw-monitor", "jarvis-telegram-alert"]
    out = subprocess.run(["systemctl", "is-active"] + svcs, capture_output=True, text=True).stdout.strip().split("\n")
    services = {s.replace("jarvis-", ""): st for s, st in zip(svcs, out)}
    active = sum(1 for v in services.values() if v == "active")
    return {"status": "ok" if active >= 4 else ("degraded" if active >= 2 else "critical"), "services": services, "active": active}


def score_health() -> dict:
    raw = r.get("jarvis:score")
    if not raw:
        return {"status": "unknown", "total": 0}
    s = json.loads(raw)
    total = s.get("total", 0)
    status = "ok" if total >= 90 else ("degraded" if total >= 70 else "critical")
    return {"status": status, "total": total}


def aggregate() -> dict:
    components = {
        "gpu": gpu_health(),
        "nodes": node_health(),
        "llm": llm_health(),
        "services": service_health(),
        "score": score_health(),
    }
    statuses = [v["status"] for v in components.values()]
    if "critical" in statuses:
        overall = "critical"
    elif "degraded" in statuses or "unknown" in statuses:
        overall = "degraded"
    else:
        overall = "ok"
    result = {
        "ts": datetime.now().isoformat()[:19],
        "overall": overall,
        "components": components,
    }
    r.setex(f"{PREFIX}:last", 60, json.dumps(result))
    return result


if __name__ == "__main__":
    res = aggregate()
    icon = {"ok": "✅", "degraded": "⚠️", "critical": "❌"}.get(res["overall"], "?")
    print(f"{icon} Overall: {res['overall']}")
    for name, comp in res["components"].items():
        s = comp["status"]
        ic = {"ok": "✅", "degraded": "⚠️", "critical": "❌", "unknown": "❓"}.get(s, "?")
        print(f"  {ic} {name}: {s}")
