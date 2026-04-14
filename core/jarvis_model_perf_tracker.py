#!/usr/bin/env python3
"""JARVIS Model Performance Tracker — Track per-model latency/quality metrics with percentiles"""

import redis
import json
import time

r = redis.Redis(decode_responses=True)

METRIC_PREFIX = "jarvis:mperf:"
STATS_KEY = "jarvis:mperf:stats"
WINDOW = 1000  # samples kept per model


def record(
    model: str,
    latency_ms: float,
    tokens: int = 0,
    success: bool = True,
    quality_score: float = None,
):
    ts = time.time()
    entry = {
        "ts": ts,
        "latency_ms": latency_ms,
        "tokens": tokens,
        "success": success,
        "quality": quality_score,
    }
    key = f"{METRIC_PREFIX}{model}:samples"
    r.lpush(key, json.dumps(entry))
    r.ltrim(key, 0, WINDOW - 1)
    r.expire(key, 86400 * 7)

    # Running counters
    r.hincrby(f"{METRIC_PREFIX}{model}:counts", "total", 1)
    if not success:
        r.hincrby(f"{METRIC_PREFIX}{model}:counts", "errors", 1)
    r.hincrby(f"{METRIC_PREFIX}{model}:counts", "tokens", tokens)

    # EWA latency
    ewa_key = f"{METRIC_PREFIX}{model}:ewa_lat"
    prev = float(r.get(ewa_key) or latency_ms)
    alpha = 0.1
    new_ewa = alpha * latency_ms + (1 - alpha) * prev
    r.setex(ewa_key, 86400, str(round(new_ewa, 1)))

    r.hincrby(STATS_KEY, "records", 1)


def percentile(values: list, p: float) -> float:
    if not values:
        return 0.0
    sorted_v = sorted(values)
    idx = (p / 100) * (len(sorted_v) - 1)
    lo, hi = int(idx), min(int(idx) + 1, len(sorted_v) - 1)
    return round(sorted_v[lo] + (idx - lo) * (sorted_v[hi] - sorted_v[lo]), 1)


def get_stats(model: str) -> dict:
    samples_raw = r.lrange(f"{METRIC_PREFIX}{model}:samples", 0, -1)
    samples = [json.loads(s) for s in samples_raw]
    if not samples:
        return {"model": model, "samples": 0}

    latencies = [s["latency_ms"] for s in samples if s.get("success")]
    counts = r.hgetall(f"{METRIC_PREFIX}{model}:counts")
    total = int(counts.get("total", 0))
    errors = int(counts.get("errors", 0))
    ewa = float(r.get(f"{METRIC_PREFIX}{model}:ewa_lat") or 0)

    qualities = [s["quality"] for s in samples if s.get("quality") is not None]

    return {
        "model": model,
        "samples": len(samples),
        "total_requests": total,
        "error_rate_pct": round(errors / max(total, 1) * 100, 1),
        "latency": {
            "p50": percentile(latencies, 50),
            "p90": percentile(latencies, 90),
            "p99": percentile(latencies, 99),
            "mean": round(sum(latencies) / max(len(latencies), 1), 1),
            "ewa": ewa,
        },
        "tokens_total": int(counts.get("tokens", 0)),
        "quality_mean": round(sum(qualities) / len(qualities), 2)
        if qualities
        else None,
    }


def compare_models(models: list) -> list:
    results = [get_stats(m) for m in models]
    results.sort(key=lambda x: x.get("latency", {}).get("p90", 9999))
    return results


def best_model(models: list, max_p90_ms: int = 5000) -> str | None:
    """Return fastest model with p90 under threshold and error_rate < 20%."""
    stats_list = compare_models(models)
    for s in stats_list:
        if (
            s.get("latency", {}).get("p90", 9999) <= max_p90_ms
            and s.get("error_rate_pct", 100) < 20
        ):
            return s["model"]
    return None


def list_models() -> list:
    keys = list(r.scan_iter(f"{METRIC_PREFIX}*:counts"))
    return [k.replace(METRIC_PREFIX, "").replace(":counts", "") for k in keys]


if __name__ == "__main__":
    import random

    models = {
        "m32_mistral": (120, 200),  # (mean_lat, std)
        "ol1_gemma3": (400, 100),
        "m2_qwen35b": (2000, 3000),  # high variance (often fails)
    }

    for model, (mean, std) in models.items():
        for _ in range(50):
            lat = max(50, random.gauss(mean, std))
            ok = random.random() > (0.3 if "m2" in model else 0.02)
            q = random.uniform(0.6, 1.0) if ok else None
            record(
                model, lat, tokens=random.randint(50, 500), success=ok, quality_score=q
            )

    print("Model Performance Report:")
    for model in models:
        s = get_stats(model)
        lat = s.get("latency", {})
        print(
            f"  {model:20s} p50={lat.get('p50'):6.0f}ms  p90={lat.get('p90'):6.0f}ms  "
            f"err={s['error_rate_pct']:4.1f}%  q={s.get('quality_mean') or '—'}"
        )

    best = best_model(list(models.keys()))
    print(f"\nBest model (p90<5s, err<20%): {best}")
    print(f"Stats: {r.hgetall(STATS_KEY)}")
