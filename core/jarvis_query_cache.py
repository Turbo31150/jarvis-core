#!/usr/bin/env python3
"""
jarvis_query_cache — Semantic query deduplication cache
Hash identical/near-identical prompts, return cached response to save tokens
"""

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.query_cache")

REDIS_PREFIX = "jarvis:qcache:"
DEFAULT_TTL = 3600  # 1 hour
MAX_CACHE_SIZE = 10000  # max entries
STATS_KEY = "jarvis:qcache:stats"


@dataclass
class CacheEntry:
    prompt_hash: str
    model: str
    response: str
    tokens_saved: int
    created_at: float
    hits: int = 0


def _hash_prompt(prompt: str, model: str) -> str:
    """Deterministic hash of (model, normalized_prompt)."""
    normalized = " ".join(prompt.lower().split())
    key = f"{model}::{normalized}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


class QueryCache:
    def __init__(self, ttl: int = DEFAULT_TTL):
        self.ttl = ttl
        self.redis: aioredis.Redis | None = None
        self._local: dict[str, CacheEntry] = {}
        self._hits = 0
        self._misses = 0

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    async def get(self, prompt: str, model: str) -> str | None:
        h = _hash_prompt(prompt, model)

        # Local cache first
        if h in self._local:
            entry = self._local[h]
            entry.hits += 1
            self._hits += 1
            log.debug(f"Cache HIT [local] {h}")
            return entry.response

        # Redis
        if self.redis:
            raw = await self.redis.get(f"{REDIS_PREFIX}{h}")
            if raw:
                entry_data = json.loads(raw)
                # Refresh in local cache
                self._local[h] = CacheEntry(
                    prompt_hash=h,
                    model=model,
                    response=entry_data["response"],
                    tokens_saved=entry_data.get("tokens_saved", 0),
                    created_at=entry_data.get("created_at", time.time()),
                    hits=entry_data.get("hits", 0) + 1,
                )
                # Update hit count in Redis
                entry_data["hits"] = self._local[h].hits
                await self.redis.set(
                    f"{REDIS_PREFIX}{h}", json.dumps(entry_data), ex=self.ttl
                )
                self._hits += 1
                log.debug(f"Cache HIT [redis] {h}")
                return entry_data["response"]

        self._misses += 1
        return None

    async def set(
        self,
        prompt: str,
        model: str,
        response: str,
        tokens_used: int = 0,
    ):
        h = _hash_prompt(prompt, model)
        entry = CacheEntry(
            prompt_hash=h,
            model=model,
            response=response,
            tokens_saved=tokens_used,
            created_at=time.time(),
        )
        self._local[h] = entry

        if self.redis:
            await self.redis.set(
                f"{REDIS_PREFIX}{h}",
                json.dumps(
                    {
                        "response": response,
                        "tokens_saved": tokens_used,
                        "created_at": entry.created_at,
                        "model": model,
                        "hits": 0,
                    }
                ),
                ex=self.ttl,
            )
        log.debug(f"Cache SET {h} ({tokens_used} tokens)")

    async def invalidate(self, prompt: str, model: str) -> bool:
        h = _hash_prompt(prompt, model)
        existed = h in self._local
        self._local.pop(h, None)
        if self.redis:
            deleted = await self.redis.delete(f"{REDIS_PREFIX}{h}")
            existed = existed or bool(deleted)
        return existed

    async def clear_model(self, model: str) -> int:
        count = 0
        # Local
        to_del = [k for k, v in self._local.items() if v.model == model]
        for k in to_del:
            del self._local[k]
            count += 1
        # Redis (scan)
        if self.redis:
            async for key in self.redis.scan_iter(f"{REDIS_PREFIX}*"):
                raw = await self.redis.get(key)
                if raw:
                    d = json.loads(raw)
                    if d.get("model") == model:
                        await self.redis.delete(key)
                        count += 1
        return count

    async def stats(self) -> dict:
        total_hits = self._hits
        total_misses = self._misses
        total = total_hits + total_misses
        hit_rate = total_hits / total if total > 0 else 0.0

        # Count Redis entries
        redis_count = 0
        if self.redis:
            redis_count = await self.redis.dbsize()

        tokens_saved = sum(e.tokens_saved * e.hits for e in self._local.values())

        return {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "hits": total_hits,
            "misses": total_misses,
            "hit_rate_pct": round(hit_rate * 100, 1),
            "local_entries": len(self._local),
            "redis_entries": redis_count,
            "tokens_saved_estimate": tokens_saved,
            "ttl_s": self.ttl,
        }

    async def save_stats(self):
        if self.redis:
            s = await self.stats()
            await self.redis.set(STATS_KEY, json.dumps(s), ex=300)


# ── CLI ───────────────────────────────────────────────────────────────────────


async def main():
    import sys

    cache = QueryCache()
    await cache.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"

    if cmd == "stats":
        s = await cache.stats()
        print(json.dumps(s, indent=2))

    elif cmd == "get" and len(sys.argv) > 3:
        prompt = sys.argv[2]
        model = sys.argv[3]
        result = await cache.get(prompt, model)
        print(f"Cache {'HIT' if result else 'MISS'}")
        if result:
            print(result[:200])

    elif cmd == "set" and len(sys.argv) > 4:
        prompt = sys.argv[2]
        model = sys.argv[3]
        response = sys.argv[4]
        await cache.set(prompt, model, response, tokens_used=50)
        print(f"Cached: {_hash_prompt(prompt, model)}")

    elif cmd == "invalidate" and len(sys.argv) > 3:
        ok = await cache.invalidate(sys.argv[2], sys.argv[3])
        print(f"Invalidated: {ok}")

    elif cmd == "clear" and len(sys.argv) > 2:
        n = await cache.clear_model(sys.argv[2])
        print(f"Cleared {n} entries for model '{sys.argv[2]}'")


if __name__ == "__main__":
    asyncio.run(main())
