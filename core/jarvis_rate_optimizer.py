#!/usr/bin/env python3
"""JARVIS Rate Optimizer — Dynamically adjust rate limits based on backend performance"""

import redis
import time
import json

r = redis.Redis(decode_responses=True)

BACKENDS = ["m1", "m2", "m32", "ol1"]
LIMITS_KEY = "jarvis:rate_optimizer:limits"
PERF_WINDOW = 300  # 5min rolling window

DEFAULTS = {
    "m1": {"rps": 5, "burst": 10},
    "m2": {"rps": 8, "burst": 15},
    "m32": {"rps": 6, "burst": 12},
    "ol1": {"rps": 10, "burst": 20},
}


def record_perf(backend: str, latency_ms: float, success: bool):
    ts = int(time.time())
    key = f"jarvis:rate_optimizer:{backend}:perf"
    r.zadd(key, {json.dumps({"ts": ts, "lat": latency_ms, "ok": success}): ts})
    r.zremrangebyscore(key, 0, ts - PERF_WINDOW)
    r.expire(key, PERF_WINDOW + 60)


def _get_perf(backend: str) -> dict:
    key = f"jarvis:rate_optimizer:{backend}:perf"
    cutoff = int(time.time()) - PERF_WINDOW
    raw = r.zrangebyscore(key, cutoff, "+inf")
    if not raw:
        return {"count": 0, "avg_lat": 0, "error_rate": 0}
    entries = [json.loads(x) for x in raw]
    lats = [e["lat"] for e in entries]
    errors = sum(1 for e in entries if not e["ok"])
    return {
        "count": len(entries),
        "avg_lat": round(sum(lats) / len(lats), 1),
        "error_rate": round(errors / len(entries), 3),
    }


def optimize() -> dict:
    """Compute optimal rate limits for each backend."""
    limits = {}
    for backend in BACKENDS:
        perf = _get_perf(backend)
        base = DEFAULTS[backend].copy()

        if perf["count"] == 0:
            limits[backend] = base
            continue

        # Scale up if fast and reliable
        if perf["avg_lat"] < 500 and perf["error_rate"] < 0.05:
            factor = 1.2
        # Scale down if slow or errors
        elif perf["avg_lat"] > 3000 or perf["error_rate"] > 0.2:
            factor = 0.6
        elif perf["error_rate"] > 0.1:
            factor = 0.8
        else:
            factor = 1.0

        limits[backend] = {
            "rps": max(1, round(base["rps"] * factor)),
            "burst": max(2, round(base["burst"] * factor)),
            "perf": perf,
        }

    r.setex(LIMITS_KEY, 300, json.dumps(limits))
    return limits


def get_limits(backend: str = None) -> dict:
    raw = r.get(LIMITS_KEY)
    all_limits = json.loads(raw) if raw else {b: DEFAULTS[b] for b in BACKENDS}
    return all_limits.get(backend, all_limits) if backend else all_limits


def stats() -> dict:
    limits = get_limits()
    perfs = {b: _get_perf(b) for b in BACKENDS}
    return {"limits": limits, "perf_5min": perfs}


if __name__ == "__main__":
    # Seed some test data
    import random

    for backend in BACKENDS:
        for _ in range(20):
            lat = random.gauss(800 if backend == "m2" else 400, 150)
            ok = random.random() > (0.15 if backend == "m2" else 0.03)
            record_perf(backend, max(50, lat), ok)

    limits = optimize()
    print("Rate Optimizer — computed limits:")
    for b, cfg in limits.items():
        perf = cfg.get("perf", {})
        print(
            f"  {b}: {cfg['rps']} rps / {cfg['burst']} burst | "
            f"avg={perf.get('avg_lat', '?')}ms err={perf.get('error_rate', '?')}"
        )
