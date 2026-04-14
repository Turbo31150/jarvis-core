#!/usr/bin/env python3
"""JARVIS Adaptive Polling — Dynamically adjust poll intervals based on system load"""
import redis, time, json
from datetime import datetime

r = redis.Redis(decode_responses=True)

# Base intervals (seconds) per component
BASE_INTERVALS = {
    "gpu_check":    30,
    "ram_check":    30,
    "llm_check":   300,
    "node_watch":   60,
    "score_update": 30,
}

def compute_interval(component: str) -> float:
    base = BASE_INTERVALS.get(component, 30)
    # Load factor: reduce interval when things are critical
    score = float(r.hget("jarvis:score", "total") or r.get("jarvis:score:total") or 80)
    if score < 50:
        factor = 0.3  # check 3x faster when score low
    elif score < 70:
        factor = 0.5
    elif score > 90:
        factor = 2.0  # relax when all good
    else:
        factor = 1.0
    return max(5, base * factor)

def get_all() -> dict:
    return {c: compute_interval(c) for c in BASE_INTERVALS}

def publish_intervals():
    intervals = get_all()
    r.setex("jarvis:adaptive:intervals", 120, json.dumps(intervals))
    return intervals

if __name__ == "__main__":
    intervals = publish_intervals()
    for k, v in intervals.items():
        print(f"  {k}: {v:.0f}s")
