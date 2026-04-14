#!/usr/bin/env python3
"""JARVIS Cache Warmer — Pre-populate caches with frequent queries to minimize cold misses"""

import redis
import time
import hashlib

r = redis.Redis(decode_responses=True)

CACHE_PREFIX = "jarvis:warmcache:"
FREQ_KEY = "jarvis:warmcache:freq"
QUEUE_KEY = "jarvis:warmcache:queue"
STATS_KEY = "jarvis:warmcache:stats"
DEFAULT_TTL = 600


def track_query(query: str, result: str = None, ttl: int = DEFAULT_TTL):
    """Track a query and optionally cache its result."""
    qhash = hashlib.md5(query.encode()).hexdigest()[:12]
    r.zincrby(FREQ_KEY, 1, qhash)
    r.hset(
        f"{CACHE_PREFIX}meta:{qhash}",
        mapping={"query": query[:500], "last_seen": time.time()},
    )
    if result is not None:
        r.setex(f"{CACHE_PREFIX}result:{qhash}", ttl, result)
        r.hincrby(STATS_KEY, "cached", 1)
    r.hincrby(STATS_KEY, "tracked", 1)


def get_cached(query: str) -> str | None:
    qhash = hashlib.md5(query.encode()).hexdigest()[:12]
    result = r.get(f"{CACHE_PREFIX}result:{qhash}")
    if result:
        r.zincrby(FREQ_KEY, 0.5, qhash)  # boost frequency on hit
        r.hincrby(STATS_KEY, "hits", 1)
        return result
    r.hincrby(STATS_KEY, "misses", 1)
    return None


def get_top_queries(limit: int = 20) -> list:
    """Return most frequent queries for pre-warming."""
    top = r.zrevrange(FREQ_KEY, 0, limit - 1, withscores=True)
    result = []
    for qhash, score in top:
        meta = r.hgetall(f"{CACHE_PREFIX}meta:{qhash}")
        cached = r.exists(f"{CACHE_PREFIX}result:{qhash}")
        result.append(
            {
                "hash": qhash,
                "query": meta.get("query", ""),
                "frequency": score,
                "is_cached": bool(cached),
                "last_seen": float(meta.get("last_seen", 0)),
            }
        )
    return result


def schedule_warmup(query: str, priority: int = 5):
    """Add query to warmup queue for background processing."""
    r.zadd(QUEUE_KEY, {query: priority})


def pop_warmup_batch(batch: int = 10) -> list:
    """Pop highest-priority queries for warmup."""
    items = r.zrevrange(QUEUE_KEY, 0, batch - 1, withscores=True)
    if items:
        r.zrem(QUEUE_KEY, *[q for q, _ in items])
    return [{"query": q, "priority": int(s)} for q, s in items]


def evict_cold(min_frequency: float = 2.0):
    """Remove cached results for queries below frequency threshold."""
    all_entries = r.zrange(FREQ_KEY, 0, -1, withscores=True)
    evicted = 0
    for qhash, score in all_entries:
        if score < min_frequency:
            r.delete(f"{CACHE_PREFIX}result:{qhash}")
            r.delete(f"{CACHE_PREFIX}meta:{qhash}")
            r.zrem(FREQ_KEY, qhash)
            evicted += 1
    r.hincrby(STATS_KEY, "evicted", evicted)
    return evicted


def hit_rate() -> float:
    hits = int(r.hget(STATS_KEY, "hits") or 0)
    misses = int(r.hget(STATS_KEY, "misses") or 0)
    total = hits + misses
    return round(hits / max(total, 1) * 100, 1)


def stats() -> dict:
    s = r.hgetall(STATS_KEY)
    return {"hit_rate_pct": hit_rate(), **{k: int(v) for k, v in s.items()}}


if __name__ == "__main__":
    # Simulate query traffic
    queries = [
        ("Quel est l'état du cluster ?", "M32 OK, M2 instable, OL1 OK"),
        ("Liste les modèles disponibles", "qwen3.5-35b, mistral-7b, gemma3:4b"),
        ("Température GPU0", "35°C"),
        ("Quel est l'état du cluster ?", None),  # repeat
        ("Liste les modèles disponibles", None),  # repeat x3
        ("Liste les modèles disponibles", None),
        ("Liste les modèles disponibles", None),
        ("VRAM disponible M32", "8.2GB/10GB"),
        ("Quel est l'état du cluster ?", None),  # repeat x2
        ("Quel est l'état du cluster ?", None),
    ]
    for q, result in queries:
        track_query(q, result, ttl=300)

    # Simulate cache lookups
    for q, _ in queries[:5]:
        hit = get_cached(q)

    top = get_top_queries(5)
    print("Top queries by frequency:")
    for entry in top:
        cached = "✅" if entry["is_cached"] else "❌"
        print(f"  {cached} [{entry['frequency']:4.1f}x] {entry['query'][:50]}")

    schedule_warmup("Température GPU0", priority=8)
    schedule_warmup("VRAM disponible M32", priority=6)
    batch = pop_warmup_batch()
    print(f"\nWarmup batch: {[b['query'] for b in batch]}")

    evicted = evict_cold(min_frequency=3.0)
    print(f"Evicted {evicted} cold entries")
    print(f"Stats: {stats()}")
