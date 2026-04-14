#!/usr/bin/env python3
"""JARVIS Quota Manager — Per-user/service request quotas with Redis TTL windows"""

import redis
import json
import time
from datetime import datetime

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:quota"

# Quota definitions: (requests, window_seconds)
QUOTAS = {
    "api_guest":    {"requests": 10,  "window_s": 60,   "burst": 3},
    "api_user":     {"requests": 100, "window_s": 60,   "burst": 20},
    "api_admin":    {"requests": 500, "window_s": 60,   "burst": 50},
    "llm_default":  {"requests": 20,  "window_s": 60,   "burst": 5},
    "llm_premium":  {"requests": 100, "window_s": 60,   "burst": 20},
    "telegram_bot": {"requests": 30,  "window_s": 60,   "burst": 10},
    "scraper":      {"requests": 50,  "window_s": 3600, "burst": 10},
}


def check_quota(identity: str, quota_type: str = "api_user") -> dict:
    """Check if identity is within quota. Returns allowed=True/False + remaining."""
    quota = QUOTAS.get(quota_type, QUOTAS["api_user"])
    window = quota["window_s"]
    limit = quota["requests"]

    key = f"{PREFIX}:{quota_type}:{identity}"
    now = time.time()
    window_start = now - window

    # Sliding window using sorted set
    pipe = r.pipeline()
    pipe.zremrangebyscore(key, 0, window_start)
    pipe.zadd(key, {str(now): now})
    pipe.zcount(key, window_start, "+inf")
    pipe.expire(key, int(window) + 10)
    _, _, count, _ = pipe.execute()

    allowed = count <= limit
    remaining = max(0, limit - count)

    if not allowed:
        r.hincrby(f"{PREFIX}:blocked:{quota_type}", identity, 1)

    return {
        "identity": identity,
        "quota_type": quota_type,
        "allowed": allowed,
        "count": count,
        "limit": limit,
        "remaining": remaining,
        "window_s": window,
    }


def get_usage(identity: str, quota_type: str = "api_user") -> dict:
    quota = QUOTAS.get(quota_type, QUOTAS["api_user"])
    key = f"{PREFIX}:{quota_type}:{identity}"
    now = time.time()
    count = r.zcount(key, now - quota["window_s"], "+inf")
    return {
        "identity": identity,
        "quota_type": quota_type,
        "current_usage": int(count),
        "limit": quota["requests"],
        "pct_used": round(int(count) / quota["requests"] * 100, 1),
    }


def stats() -> dict:
    result = {}
    for qt in QUOTAS:
        blocked = r.hgetall(f"{PREFIX}:blocked:{qt}")
        total_blocked = sum(int(v) for v in blocked.values())
        result[qt] = {"blocked_requests": total_blocked, "blocked_identities": len(blocked)}
    return result


if __name__ == "__main__":
    # Test quota enforcement
    results = []
    for i in range(12):
        res = check_quota("test_user", "api_guest")  # limit=10
        results.append(res["allowed"])

    blocked = results.count(False)
    allowed = results.count(True)
    print(f"api_guest (limit=10): {allowed} allowed, {blocked} blocked out of 12 requests")
    usage = get_usage("test_user", "api_guest")
    print(f"Usage: {usage['current_usage']}/{usage['limit']} ({usage['pct_used']}%)")
    print(f"Stats: {stats()}")
