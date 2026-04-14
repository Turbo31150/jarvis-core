#!/usr/bin/env python3
"""JARVIS Drift Detector — Detect model/metric drift using statistical tests"""

import redis
import json
import time
import statistics

r = redis.Redis(decode_responses=True)

BASELINE_PREFIX = "jarvis:drift:baseline:"
CURRENT_PREFIX = "jarvis:drift:current:"
ALERTS_KEY = "jarvis:drift:alerts"
WINDOW = 50  # samples per window


def record_baseline(metric: str, values: list):
    """Set the baseline distribution for a metric."""
    if len(values) < 5:
        return False
    stats = {
        "mean": statistics.mean(values),
        "stdev": statistics.stdev(values) if len(values) > 1 else 0,
        "p25": sorted(values)[len(values) // 4],
        "p75": sorted(values)[3 * len(values) // 4],
        "n": len(values),
        "recorded_at": time.time(),
    }
    r.set(f"{BASELINE_PREFIX}{metric}", json.dumps(stats))
    return stats


def record_value(metric: str, value: float):
    """Record a current value for drift monitoring."""
    key = f"{CURRENT_PREFIX}{metric}"
    r.rpush(key, value)
    r.ltrim(key, -WINDOW, -1)
    r.expire(key, 3600)


def detect_drift(metric: str, threshold_sigma: float = 2.5) -> dict:
    """Detect if current distribution has drifted from baseline."""
    raw_baseline = r.get(f"{BASELINE_PREFIX}{metric}")
    if not raw_baseline:
        return {"drift": False, "reason": "no baseline"}

    baseline = json.loads(raw_baseline)
    raw_current = r.lrange(f"{CURRENT_PREFIX}{metric}", 0, -1)
    if len(raw_current) < 5:
        return {"drift": False, "reason": "insufficient current data"}

    current_vals = [float(x) for x in raw_current]
    cur_mean = statistics.mean(current_vals)
    cur_stdev = statistics.stdev(current_vals) if len(current_vals) > 1 else 0

    base_mean = baseline["mean"]
    base_stdev = baseline["stdev"] or 0.01  # avoid division by zero

    # Z-score of current mean vs baseline
    z_score = abs(cur_mean - base_mean) / base_stdev
    # Variance ratio
    var_ratio = (cur_stdev / base_stdev) if base_stdev > 0 else 1.0

    drifted = z_score > threshold_sigma or var_ratio > 3.0
    result = {
        "metric": metric,
        "drift": drifted,
        "z_score": round(z_score, 2),
        "variance_ratio": round(var_ratio, 2),
        "baseline_mean": round(base_mean, 3),
        "current_mean": round(cur_mean, 3),
        "delta_pct": round(
            (cur_mean - base_mean) / max(abs(base_mean), 0.001) * 100, 1
        ),
        "n_current": len(current_vals),
    }
    if drifted:
        r.lpush(ALERTS_KEY, json.dumps({**result, "ts": time.time()}))
        r.ltrim(ALERTS_KEY, 0, 99)
    return result


def scan_all() -> list:
    """Scan all metrics with baselines for drift."""
    keys = r.keys(f"{BASELINE_PREFIX}*")
    results = []
    for key in keys:
        metric = key.replace(BASELINE_PREFIX, "")
        result = detect_drift(metric)
        if result.get("drift") or result.get("z_score", 0) > 1.0:
            results.append(result)
    return sorted(results, key=lambda x: x.get("z_score", 0), reverse=True)


def recent_alerts(limit: int = 10) -> list:
    raw = r.lrange(ALERTS_KEY, 0, limit - 1)
    return [json.loads(x) for x in raw]


def stats() -> dict:
    n_baselines = len(r.keys(f"{BASELINE_PREFIX}*"))
    n_alerts = r.llen(ALERTS_KEY)
    return {
        "baselines": n_baselines,
        "total_alerts": n_alerts,
        "drifted_now": scan_all(),
    }


if __name__ == "__main__":
    import random

    # Establish baselines
    for metric, base_vals in [
        ("llm_latency_m2", [800 + random.gauss(0, 80) for _ in range(30)]),
        ("gpu_temp", [35 + random.gauss(0, 2) for _ in range(30)]),
        ("error_rate", [0.02 + random.gauss(0, 0.005) for _ in range(30)]),
    ]:
        record_baseline(metric, base_vals)

    # Simulate current — M2 latency has drifted up
    for _ in range(20):
        record_value("llm_latency_m2", 1800 + random.gauss(0, 100))  # 2x baseline
        record_value("gpu_temp", 36 + random.gauss(0, 2))  # normal
        record_value("error_rate", 0.021 + random.gauss(0, 0.005))  # normal

    for metric in ["llm_latency_m2", "gpu_temp", "error_rate"]:
        result = detect_drift(metric)
        icon = "🚨" if result["drift"] else "✅"
        print(
            f"  {icon} {metric}: z={result.get('z_score', 0):.2f} "
            f"delta={result.get('delta_pct', 0):+.1f}%"
        )
