#!/usr/bin/env python3
"""JARVIS Token Tracker — Track tokens used per model per day (cost awareness)"""
import redis, json
from datetime import datetime

r = redis.Redis(decode_responses=True)

COSTS_PER_1K = {
    "claude": 0.015,    # Sonnet
    "gpt-4": 0.030,
    "gemini": 0.000,    # free
    "local": 0.000,     # local = free
    "openrouter": 0.001,
}

def track(model: str, tokens_in: int, tokens_out: int, backend: str = "local"):
    today = datetime.now().strftime("%Y-%m-%d")
    r.hincrby(f"jarvis:tokens:{today}:{backend}", "tokens_in", tokens_in)
    r.hincrby(f"jarvis:tokens:{today}:{backend}", "tokens_out", tokens_out)
    r.expire(f"jarvis:tokens:{today}:{backend}", 86400 * 30)
    
    cost_key = COSTS_PER_1K.get(backend, 0)
    cost = (tokens_in + tokens_out) / 1000 * cost_key
    r.hincrbyfloat(f"jarvis:tokens:{today}:{backend}", "cost_usd", cost)

def daily_report(date: str = None) -> dict:
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")
    report = {}
    for key in r.scan_iter(f"jarvis:tokens:{date}:*"):
        backend = key.split(":")[-1]
        report[backend] = {k: float(v) for k, v in r.hgetall(key).items()}
    return report

if __name__ == "__main__":
    track("claude-sonnet", 1000, 500, "claude")
    track("gemma3:4b", 200, 100, "local")
    print("Today:", daily_report())
