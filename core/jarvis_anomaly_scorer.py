#!/usr/bin/env python3
"""JARVIS Anomaly Scorer — Score system anomalies using Z-score + IQR methods"""

import redis
import json
import math
import time
from datetime import datetime

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:anomaly"

METRICS_TO_WATCH = {
    "jarvis:score": {"field": "total", "threshold_z": 2.5, "lower_is_bad": True},
    "jarvis:gpu:0:temp": {"threshold_z": 2.5, "lower_is_bad": False},
    "jarvis:gpu:1:temp": {"threshold_z": 2.5, "lower_is_bad": False},
    "jarvis:gpu:2:temp": {"threshold_z": 2.5, "lower_is_bad": False},
}

WINDOW = 50  # samples for stats


def push_sample(metric: str, value: float):
    key = f"{PREFIX}:series:{metric.replace(':', '_')}"
    r.lpush(key, value)
    r.ltrim(key, 0, WINDOW - 1)
    r.expire(key, 86400)


def get_samples(metric: str) -> list[float]:
    key = f"{PREFIX}:series:{metric.replace(':', '_')}"
    raw = r.lrange(key, 0, -1)
    return [float(v) for v in raw]


def zscore(value: float, samples: list) -> float:
    if len(samples) < 3:
        return 0.0
    mean = sum(samples) / len(samples)
    variance = sum((x - mean) ** 2 for x in samples) / len(samples)
    std = math.sqrt(variance)
    return (value - mean) / std if std > 0 else 0.0


def iqr_check(value: float, samples: list) -> bool:
    if len(samples) < 4:
        return False
    sorted_s = sorted(samples)
    n = len(sorted_s)
    q1 = sorted_s[n // 4]
    q3 = sorted_s[3 * n // 4]
    iqr = q3 - q1
    return value < q1 - 1.5 * iqr or value > q3 + 1.5 * iqr


def scan_anomalies() -> dict:
    anomalies = []
    # Collect current values
    score_raw = r.get("jarvis:score")
    if score_raw:
        score_val = json.loads(score_raw).get("total", 0)
        push_sample("score_total", score_val)
        samples = get_samples("score_total")
        z = zscore(score_val, samples)
        if abs(z) > 2.5 and score_val < 70:
            anomalies.append({"metric": "jarvis_score", "value": score_val, "z_score": round(z, 2), "severity": "critical" if score_val < 50 else "warning"})

    for i in range(5):
        temp = r.get(f"jarvis:gpu:{i}:temp")
        if temp:
            t = float(temp)
            push_sample(f"gpu{i}_temp", t)
            samples = get_samples(f"gpu{i}_temp")
            z = zscore(t, samples)
            is_iqr = iqr_check(t, samples)
            if (abs(z) > 2.5 or is_iqr) and t > 75:
                anomalies.append({"metric": f"gpu{i}_temp", "value": t, "z_score": round(z, 2), "iqr_outlier": is_iqr, "severity": "critical" if t > 82 else "warning"})

    result = {
        "ts": datetime.now().isoformat()[:19],
        "anomalies": anomalies,
        "count": len(anomalies),
        "status": "alert" if anomalies else "normal",
    }
    r.setex(f"{PREFIX}:last", 120, json.dumps(result))
    if anomalies:
        for a in anomalies:
            r.publish("jarvis:alerts", json.dumps({"type": "anomaly_detected", "severity": a["severity"], "data": a, "ts": result["ts"]}))
    return result


def stats() -> dict:
    metrics_tracked = len([k for k in r.scan_iter(f"{PREFIX}:series:*")])
    last = json.loads(r.get(f"{PREFIX}:last") or "{}")
    return {"metrics_tracked": metrics_tracked, "last_status": last.get("status", "unknown"), "last_count": last.get("count", 0)}


if __name__ == "__main__":
    # Seed some history first
    for i in range(20):
        push_sample("score_total", 100 - i * 0.1)
        for g in range(5):
            push_sample(f"gpu{g}_temp", 32 + g + i * 0.05)
    res = scan_anomalies()
    print(f"Anomaly scan: {res['status']} ({res['count']} anomalies)")
    if res["anomalies"]:
        for a in res["anomalies"]:
            print(f"  ⚠️ {a['metric']}: {a['value']} (z={a['z_score']})")
    else:
        print("  ✅ All metrics normal")
    print(f"Stats: {stats()}")
