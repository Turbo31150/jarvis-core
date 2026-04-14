#!/usr/bin/env python3
"""JARVIS Health Scorer — Composite health score with weighted dimensions and trend"""

import redis
import json
import time

r = redis.Redis(decode_responses=True)

SCORE_KEY = "jarvis:health_scorer:score"
HISTORY_KEY = "jarvis:health_scorer:history"

DIMENSIONS = {
    "gpu_thermal": {
        "weight": 0.20,
        "green": (0, 70),
        "yellow": (70, 82),
        "red": (82, 200),
    },
    "ram_free_pct": {
        "weight": 0.15,
        "green": (40, 100),
        "yellow": (20, 40),
        "red": (0, 20),
    },
    "cpu_idle_pct": {
        "weight": 0.15,
        "green": (30, 100),
        "yellow": (10, 30),
        "red": (0, 10),
    },
    "llm_error_rate": {
        "weight": 0.20,
        "green": (0, 0.05),
        "yellow": (0.05, 0.2),
        "red": (0.2, 1.0),
    },
    "nodes_up_pct": {
        "weight": 0.15,
        "green": (0.8, 1.0),
        "yellow": (0.5, 0.8),
        "red": (0, 0.5),
    },
    "queue_depth": {
        "weight": 0.10,
        "green": (0, 100),
        "yellow": (100, 500),
        "red": (500, 99999),
    },
    "redis_mem_mb": {
        "weight": 0.05,
        "green": (0, 200),
        "yellow": (200, 400),
        "red": (400, 99999),
    },
}


def _dim_score(name: str, value: float) -> float:
    """Score a single dimension: 100=green, 60=yellow, 20=red."""
    d = DIMENSIONS[name]
    lo_g, hi_g = d["green"]
    lo_y, hi_y = d["yellow"]

    in_green = lo_g <= value <= hi_g
    in_yellow = lo_y <= value <= hi_y

    if in_green:
        return 100.0
    elif in_yellow:
        # Linear interpolation within yellow zone
        span = hi_y - lo_y if hi_y != lo_y else 1
        pos = (value - lo_y) / span
        # Closer to green boundary = higher score
        dist_to_green = min(abs(value - lo_g), abs(value - hi_g)) / span
        return 60.0 + dist_to_green * 40
    else:
        return 20.0


def compute(metrics: dict) -> dict:
    """Compute composite health score from raw metrics."""
    scores = {}
    total_weight = 0.0
    weighted_sum = 0.0

    for dim, cfg in DIMENSIONS.items():
        if dim not in metrics:
            continue
        s = _dim_score(dim, metrics[dim])
        scores[dim] = round(s, 1)
        weighted_sum += s * cfg["weight"]
        total_weight += cfg["weight"]

    composite = round(weighted_sum / max(total_weight, 0.01))
    status = "green" if composite >= 80 else ("yellow" if composite >= 55 else "red")

    result = {
        "score": composite,
        "status": status,
        "dimensions": scores,
        "ts": time.time(),
    }
    r.setex(SCORE_KEY, 300, json.dumps(result))
    r.lpush(HISTORY_KEY, json.dumps({"score": composite, "ts": time.time()}))
    r.ltrim(HISTORY_KEY, 0, 287)  # 24h at 5min intervals
    return result


def trend(periods: int = 12) -> dict:
    """Get score trend over last N periods."""
    raw = r.lrange(HISTORY_KEY, 0, periods - 1)
    if not raw:
        return {"trend": "unknown", "delta": 0}
    points = [json.loads(x)["score"] for x in raw]
    if len(points) < 2:
        return {"trend": "stable", "delta": 0}
    delta = points[0] - points[-1]
    trend_dir = "improving" if delta > 5 else ("degrading" if delta < -5 else "stable")
    return {
        "trend": trend_dir,
        "delta": round(delta, 1),
        "min": min(points),
        "max": max(points),
    }


def get() -> dict:
    raw = r.get(SCORE_KEY)
    return json.loads(raw) if raw else {"score": 0, "status": "unknown"}


def stats() -> dict:
    return {**get(), "trend": trend()}


if __name__ == "__main__":
    test_metrics = {
        "gpu_thermal": 36.0,
        "ram_free_pct": 58.0,
        "cpu_idle_pct": 55.0,
        "llm_error_rate": 0.03,
        "nodes_up_pct": 0.8,
        "queue_depth": 12,
        "redis_mem_mb": 45.0,
    }
    result = compute(test_metrics)
    print(f"Health Score: {result['score']}/100 [{result['status'].upper()}]")
    for dim, s in result["dimensions"].items():
        icon = "🟢" if s >= 80 else ("🟡" if s >= 55 else "🔴")
        print(f"  {icon} {dim:20s}: {s:.0f}")
    print(f"Trend: {trend()}")
