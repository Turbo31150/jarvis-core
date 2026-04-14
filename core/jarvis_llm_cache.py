#!/usr/bin/env python3
"""JARVIS LLM Cache — Semantic cache to avoid redundant LLM calls"""

import redis
import hashlib
import json
import time
from datetime import datetime

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:llm_cache"
DEFAULT_TTL = 3600  # 1 hour


def _make_key(prompt: str, model: str = "", task_type: str = "") -> str:
    """Deterministic cache key from prompt + model + type"""
    normalized = prompt.strip().lower()
    raw = f"{model}:{task_type}:{normalized}"
    return f"{PREFIX}:{hashlib.sha256(raw.encode()).hexdigest()[:16]}"


def get(prompt: str, model: str = "", task_type: str = "") -> str | None:
    key = _make_key(prompt, model, task_type)
    cached = r.get(key)
    if cached:
        r.hincrby(f"{PREFIX}:stats", "hits", 1)
        return json.loads(cached)["response"]
    r.hincrby(f"{PREFIX}:stats", "misses", 1)
    return None


def set_cache(prompt: str, response: str, model: str = "", task_type: str = "", ttl: int = DEFAULT_TTL):
    key = _make_key(prompt, model, task_type)
    payload = {
        "prompt_preview": prompt[:80],
        "response": response,
        "model": model,
        "task_type": task_type,
        "cached_at": datetime.now().isoformat()[:19],
    }
    r.setex(key, ttl, json.dumps(payload))
    r.hincrby(f"{PREFIX}:stats", "stored", 1)


def cached_ask(prompt: str, model: str = "", task_type: str = "", ask_fn=None) -> tuple[str, bool]:
    """Try cache first, fall back to ask_fn. Returns (response, was_cached)"""
    cached = get(prompt, model, task_type)
    if cached is not None:
        return cached, True
    if ask_fn is None:
        return "", False
    response = ask_fn(prompt)
    if response:
        set_cache(prompt, response, model, task_type)
    return response, False


def invalidate_pattern(pattern: str = "*") -> int:
    deleted = 0
    for key in r.scan_iter(f"{PREFIX}:{pattern}"):
        r.delete(key)
        deleted += 1
    return deleted


def stats() -> dict:
    data = r.hgetall(f"{PREFIX}:stats")
    hits = int(data.get("hits", 0))
    misses = int(data.get("misses", 0))
    total = hits + misses
    hit_rate = round(hits / total * 100, 1) if total > 0 else 0.0
    # Count cached entries
    cached_count = sum(1 for _ in r.scan_iter(f"{PREFIX}:????????????????"))
    return {
        "hits": hits,
        "misses": misses,
        "hit_rate_pct": hit_rate,
        "stored": int(data.get("stored", 0)),
        "cached_entries": cached_count,
    }


if __name__ == "__main__":
    # Test
    set_cache("2+2=?", "4", model="gemma3", task_type="fast")
    set_cache("What is Redis?", "Redis is an in-memory data store", model="gemma3", task_type="fast")

    r1 = get("2+2=?", "gemma3", "fast")
    r2 = get("2+2=?", "gemma3", "fast")  # cache hit
    r3 = get("unknown question", "gemma3", "fast")  # cache miss
    print(f"First: {r1}")
    print(f"Second (should be same): {r2}")
    print(f"Miss: {r3}")
    print(f"Stats: {stats()}")
