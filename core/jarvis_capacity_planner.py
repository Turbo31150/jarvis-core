#!/usr/bin/env python3
"""JARVIS Capacity Planner — Forecast resource needs based on usage trends"""

import redis
import json
import time
from datetime import datetime, timedelta

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:capacity"


def record_sample(metric: str, value: float):
    ts = time.time()
    r.zadd(f"{PREFIX}:series:{metric}", {json.dumps({"ts": ts, "v": value}): ts})
    # Keep 7 days
    r.zremrangebyscore(f"{PREFIX}:series:{metric}", 0, ts - 86400 * 7)


def get_series(metric: str, hours: int = 24) -> list:
    since = time.time() - hours * 3600
    raw = r.zrangebyscore(f"{PREFIX}:series:{metric}", since, "+inf")
    return [json.loads(e) for e in raw]


def linear_trend(values: list) -> float:
    """Simple linear regression slope (change per unit)"""
    n = len(values)
    if n < 2:
        return 0.0
    x_mean = (n - 1) / 2
    y_mean = sum(values) / n
    num = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(values))
    den = sum((i - x_mean) ** 2 for i in range(n))
    return num / den if den != 0 else 0.0


def forecast(metric: str, hours_ahead: int = 24) -> dict:
    series = get_series(metric, hours=72)
    if len(series) < 3:
        return {"metric": metric, "forecast": None, "reason": "insufficient_data"}
    values = [s["v"] for s in series]
    current = values[-1]
    slope = linear_trend(values)
    # Project forward (slope per sample, assume 1 sample per 5 min)
    samples_ahead = hours_ahead * 12
    projected = current + slope * samples_ahead
    return {
        "metric": metric,
        "current": round(current, 2),
        "projected": round(projected, 2),
        "slope_per_hour": round(slope * 12, 4),
        "hours_ahead": hours_ahead,
        "trend": "increasing" if slope > 0.01 else ("decreasing" if slope < -0.01 else "stable"),
    }


def capacity_report() -> dict:
    """Full capacity report for all tracked metrics"""
    score = json.loads(r.get("jarvis:score") or "{}")
    # Record current samples
    record_sample("ram_used_pct", 100 - float(score.get("ram", 0)) * 5)
    record_sample("gpu_max_temp", float(r.get("jarvis:gpu:max_temp") or score.get("gpu_max_temp", 35)))
    record_sample("jarvis_score", float(score.get("total", 0)))

    metrics = ["ram_used_pct", "gpu_max_temp", "jarvis_score"]
    forecasts = {m: forecast(m, 24) for m in metrics}

    # Risk assessment
    risks = []
    ram_proj = forecasts["ram_used_pct"].get("projected")
    if ram_proj and ram_proj > 85:
        risks.append({"resource": "RAM", "risk": "high", "projected_pct": round(ram_proj, 1)})
    gpu_proj = forecasts["gpu_max_temp"].get("projected")
    if gpu_proj and gpu_proj > 78:
        risks.append({"resource": "GPU_TEMP", "risk": "medium", "projected_c": round(gpu_proj, 1)})
    score_proj = forecasts["jarvis_score"].get("projected")
    if score_proj and score_proj < 70:
        risks.append({"resource": "SCORE", "risk": "high", "projected": round(score_proj, 1)})

    return {
        "ts": datetime.now().isoformat()[:19],
        "forecasts": forecasts,
        "risks": risks,
        "overall_risk": "high" if any(r["risk"] == "high" for r in risks) else ("medium" if risks else "low"),
    }


if __name__ == "__main__":
    # Seed some data
    for i in range(10):
        record_sample("ram_used_pct", 42 + i * 0.5)
        record_sample("gpu_max_temp", 36 + i * 0.1)
        record_sample("jarvis_score", 100 - i * 0.2)
        time.sleep(0.01)

    rep = capacity_report()
    print(f"Capacity Report — Risk: {rep['overall_risk']}")
    for m, f in rep["forecasts"].items():
        if f.get("forecast") is None and "insufficient_data" not in str(f):
            print(f"  {m}: current={f.get('current')} → projected={f.get('projected')} ({f.get('trend')})")
        elif f.get("projected"):
            print(f"  {m}: current={f.get('current')} → projected={f.get('projected')} ({f.get('trend')})")
    if rep["risks"]:
        print(f"Risks:")
        for risk in rep["risks"]:
            print(f"  ⚠️  {risk}")
    else:
        print("No capacity risks detected")
