#!/usr/bin/env python3
"""JARVIS Prompt Cache — Semantic deduplication and exact-match caching for LLM prompts"""

import redis
import json
import time
import hashlib

r = redis.Redis(decode_responses=True)

EXACT_PREFIX = "jarvis:pcache:exact:"
STATS_KEY = "jarvis:pcache:stats"
TTL_DEFAULT = 3600  # 1h


def _exact_key(prompt: str, model: str = "", task_type: str = "") -> str:
    raw = f"{prompt}|{model}|{task_type}"
    return EXACT_PREFIX + hashlib.sha256(raw.encode()).hexdigest()[:32]


def get(prompt: str, model: str = "", task_type: str = "") -> str | None:
    key = _exact_key(prompt, model, task_type)
    raw = r.get(key)
    if raw:
        r.hincrby(STATS_KEY, "hits", 1)
        data = json.loads(raw)
        data["hit_count"] = int(data.get("hit_count", 0)) + 1
        r.setex(key, TTL_DEFAULT, json.dumps(data))
        return data["response"]
    r.hincrby(STATS_KEY, "misses", 1)
    return None


def put(
    prompt: str,
    response: str,
    model: str = "",
    task_type: str = "",
    ttl: int = TTL_DEFAULT,
    latency_ms: float = 0,
):
    key = _exact_key(prompt, model, task_type)
    data = {
        "prompt": prompt[:300],
        "response": response,
        "model": model,
        "task_type": task_type,
        "latency_ms": latency_ms,
        "cached_at": time.time(),
        "hit_count": 0,
    }
    r.setex(key, ttl, json.dumps(data))
    r.hincrby(STATS_KEY, "stored", 1)


def invalidate(prompt: str, model: str = "", task_type: str = "") -> bool:
    key = _exact_key(prompt, model, task_type)
    deleted = r.delete(key)
    return deleted > 0


def warmup(entries: list):
    """Pre-populate cache with known prompt/response pairs."""
    for entry in entries:
        put(
            entry["prompt"],
            entry["response"],
            entry.get("model", ""),
            entry.get("task_type", ""),
            entry.get("ttl", TTL_DEFAULT),
        )
    return len(entries)


def stats() -> dict:
    s = r.hgetall(STATS_KEY)
    hits = int(s.get("hits", 0))
    misses = int(s.get("misses", 0))
    total = hits + misses
    return {
        "hits": hits,
        "misses": misses,
        "stored": int(s.get("stored", 0)),
        "hit_rate": round(hits / max(total, 1), 3),
    }


if __name__ == "__main__":
    # Warmup common queries
    warmup(
        [
            {
                "prompt": "What is 2+2?",
                "response": "4",
                "model": "ol1",
                "task_type": "fast",
            },
            {
                "prompt": "Current GPU status",
                "response": "All GPUs nominal, max 36°C",
                "model": "m32",
            },
            {
                "prompt": "JARVIS health",
                "response": "Score 95/100, all systems operational",
                "task_type": "classify",
            },
        ]
    )

    # Test hits/misses
    tests = [
        ("What is 2+2?", "ol1", "fast"),
        ("What is 3+3?", "ol1", "fast"),
        ("Current GPU status", "", ""),
        ("JARVIS health", "", "classify"),
    ]
    for prompt, model, task in tests:
        result = get(prompt, model, task)
        icon = "HIT" if result else "MISS"
        print(f"  [{icon}] {prompt[:30]} → {str(result)[:40] if result else '-'}")

    print(f"\nStats: {stats()}")
