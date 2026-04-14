#!/usr/bin/env python3
"""JARVIS Time Series — Store and query time series data with aggregations"""

import redis
import json
import time
from datetime import datetime, timedelta

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:ts"

RESOLUTIONS = {
    "1m":  60,
    "5m":  300,
    "1h":  3600,
    "1d":  86400,
}

RETENTION = {
    "1m":  3600 * 6,    # 6h of 1m data
    "5m":  86400,       # 1d of 5m data
    "1h":  86400 * 7,   # 7d of 1h data
    "1d":  86400 * 90,  # 90d of 1d data
}


def record(metric: str, value: float, timestamp: float = None):
    ts = timestamp or time.time()
    for res, bucket_size in RESOLUTIONS.items():
        bucket = int(ts // bucket_size) * bucket_size
        key = f"{PREFIX}:{res}:{metric}:{bucket}"
        # Store as running average: [sum, count]
        pipe = r.pipeline()
        pipe.hincrbyfloat(key, "sum", value)
        pipe.hincrby(key, "count", 1)
        pipe.hset(key, "last", value)
        pipe.expire(key, RETENTION[res])
        pipe.execute()


def query(metric: str, resolution: str = "5m", hours: int = 24) -> list:
    bucket_size = RESOLUTIONS.get(resolution, 300)
    now = time.time()
    start = now - hours * 3600
    start_bucket = int(start // bucket_size) * bucket_size
    end_bucket = int(now // bucket_size) * bucket_size
    result = []
    b = start_bucket
    while b <= end_bucket:
        key = f"{PREFIX}:{resolution}:{metric}:{b}"
        data = r.hgetall(key)
        if data:
            count = int(data.get("count", 1))
            avg = float(data.get("sum", 0)) / count
            result.append({
                "ts": b,
                "dt": datetime.fromtimestamp(b).strftime("%H:%M"),
                "avg": round(avg, 2),
                "last": float(data.get("last", avg)),
                "count": count,
            })
        b += bucket_size
    return result


def latest(metric: str, resolution: str = "1m") -> float | None:
    now = time.time()
    bucket_size = RESOLUTIONS[resolution]
    bucket = int(now // bucket_size) * bucket_size
    key = f"{PREFIX}:{resolution}:{metric}:{bucket}"
    val = r.hget(key, "last")
    return float(val) if val else None


def aggregate(metric: str, resolution: str = "1h", func: str = "avg") -> dict:
    data = query(metric, resolution, 24)
    if not data:
        return {"metric": metric, "resolution": resolution, "no_data": True}
    values = [d["avg"] for d in data]
    return {
        "metric": metric,
        "resolution": resolution,
        "points": len(values),
        "min": round(min(values), 2),
        "max": round(max(values), 2),
        "avg": round(sum(values)/len(values), 2),
        "last": data[-1]["avg"],
    }


def seed_from_redis():
    """Seed time series from current Redis metrics"""
    score = json.loads(r.get("jarvis:score") or "{}")
    if score:
        record("jarvis_score", float(score.get("total", 0)))
        record("cpu_temp", float(score.get("cpu_temp", 0)))
        record("ram_free_gb", float(score.get("ram_free_gb", 0)))
    for i in range(5):
        temp = r.get(f"jarvis:gpu:{i}:temp")
        if temp:
            record(f"gpu{i}_temp", float(temp))
    return True


if __name__ == "__main__":
    # Seed with fake history
    now = time.time()
    for i in range(20):
        ts = now - i * 300  # every 5 min back
        record("jarvis_score", 100 - i * 0.5, ts)
        record("gpu0_temp", 36 + i * 0.2, ts)

    seed_from_redis()

    agg = aggregate("jarvis_score", "5m")
    print(f"jarvis_score: min={agg['min']} max={agg['max']} avg={agg['avg']} ({agg['points']} points)")
    agg2 = aggregate("gpu0_temp", "5m")
    print(f"gpu0_temp: min={agg2['min']} max={agg2['max']} avg={agg2['avg']}")
    latest_score = latest("jarvis_score")
    print(f"Latest score: {latest_score}")
