#!/usr/bin/env python3
"""JARVIS Cost Tracker — Track LLM token costs per model, per day, per task type"""

import redis
import json
import time
from datetime import datetime, timedelta

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:cost"

# Pricing per 1M tokens (USD) — update as needed
PRICING = {
    "claude-opus":      {"input": 15.0,  "output": 75.0},
    "claude-sonnet":    {"input": 3.0,   "output": 15.0},
    "claude-haiku":     {"input": 0.25,  "output": 1.25},
    "gpt-4o":           {"input": 2.5,   "output": 10.0},
    "gpt-4o-mini":      {"input": 0.15,  "output": 0.60},
    "local":            {"input": 0.0,   "output": 0.0},
    "gemini-flash":     {"input": 0.075, "output": 0.30},
    "gemini-pro":       {"input": 1.25,  "output": 5.0},
}


def record(model: str, input_tokens: int, output_tokens: int, task_type: str = "default"):
    pricing = PRICING.get(model, PRICING["local"])
    cost = (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000
    today = datetime.now().strftime("%Y-%m-%d")
    key = f"{PREFIX}:{today}:{model}"
    pipe = r.pipeline()
    pipe.hincrbyfloat(key, "cost_usd", cost)
    pipe.hincrby(key, "input_tokens", input_tokens)
    pipe.hincrby(key, "output_tokens", output_tokens)
    pipe.hincrby(key, "calls", 1)
    pipe.expire(key, 86400 * 30)  # keep 30 days
    pipe.execute()
    # Also track by task type
    tkey = f"{PREFIX}:task:{today}:{task_type}"
    r.hincrbyfloat(tkey, "cost_usd", cost)
    r.hincrby(tkey, "calls", 1)
    r.expire(tkey, 86400 * 30)
    return cost


def daily_report(date: str = None) -> dict:
    day = date or datetime.now().strftime("%Y-%m-%d")
    total_cost = 0.0
    total_tokens = 0
    by_model = {}
    for key in r.scan_iter(f"{PREFIX}:{day}:*"):
        parts = key.split(":")
        if len(parts) < 4:
            continue
        model = parts[3]
        data = r.hgetall(key)
        cost = float(data.get("cost_usd", 0))
        tokens = int(data.get("input_tokens", 0)) + int(data.get("output_tokens", 0))
        total_cost += cost
        total_tokens += tokens
        by_model[model] = {"cost_usd": round(cost, 6), "tokens": tokens, "calls": int(data.get("calls", 0))}
    return {
        "date": day,
        "total_cost_usd": round(total_cost, 6),
        "total_tokens": total_tokens,
        "by_model": by_model,
    }


def weekly_summary() -> dict:
    days = []
    total = 0.0
    for i in range(7):
        d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        rep = daily_report(d)
        total += rep["total_cost_usd"]
        days.append({"date": d, "cost_usd": rep["total_cost_usd"], "tokens": rep["total_tokens"]})
    return {"week_total_usd": round(total, 4), "days": days}


if __name__ == "__main__":
    # Record some test entries
    record("claude-sonnet", 1000, 500, "code")
    record("local", 5000, 2000, "fast")
    record("gemini-flash", 800, 300, "summary")
    rep = daily_report()
    print(f"Today: ${rep['total_cost_usd']:.6f} | {rep['total_tokens']} tokens")
    for m, d in rep["by_model"].items():
        print(f"  {m}: ${d['cost_usd']:.6f} ({d['calls']} calls)")
    weekly = weekly_summary()
    print(f"Week total: ${weekly['week_total_usd']:.4f}")
