#!/usr/bin/env python3
"""
jarvis_cache_invalidator — Smart cache invalidation with dependency tracking
Invalidates related cache keys when source data changes, supports TTL-based and event-based expiry
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Callable

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.cache_invalidator")

REDIS_PREFIX = "jarvis:cache:"
REDIS_DEPS = "jarvis:cache:deps"
REDIS_CHANNEL = "jarvis:cache:invalidate"


@dataclass
class CacheEntry:
    key: str
    value: str
    ttl_s: float
    tags: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    hits: int = 0

    @property
    def expired(self) -> bool:
        return time.time() - self.created_at > self.ttl_s


class CacheInvalidator:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._local: dict[str, CacheEntry] = {}
        self._tag_map: dict[str, set[str]] = {}  # tag → set of keys
        self._on_invalidate: list[Callable] = []

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def on_invalidate(self, callback: Callable):
        self._on_invalidate.append(callback)

    # ── Write ──────────────────────────────────────────────────────────────────

    async def set(
        self,
        key: str,
        value: str,
        ttl_s: float = 300.0,
        tags: list[str] | None = None,
    ):
        tags = tags or []
        entry = CacheEntry(key=key, value=value, ttl_s=ttl_s, tags=tags)
        self._local[key] = entry

        # Update tag → keys mapping
        for tag in tags:
            self._tag_map.setdefault(tag, set()).add(key)

        if self.redis:
            await self.redis.setex(f"{REDIS_PREFIX}{key}", int(ttl_s), value)
            if tags:
                pipe = self.redis.pipeline()
                for tag in tags:
                    pipe.sadd(f"{REDIS_DEPS}:tag:{tag}", key)
                    pipe.expire(f"{REDIS_DEPS}:tag:{tag}", int(ttl_s) + 60)
                await pipe.execute()

    # ── Read ───────────────────────────────────────────────────────────────────

    async def get(self, key: str) -> str | None:
        # Check local first
        entry = self._local.get(key)
        if entry:
            if not entry.expired:
                entry.hits += 1
                return entry.value
            else:
                del self._local[key]

        # Check Redis
        if self.redis:
            val = await self.redis.get(f"{REDIS_PREFIX}{key}")
            if val:
                return val

        return None

    async def exists(self, key: str) -> bool:
        return await self.get(key) is not None

    # ── Invalidation ───────────────────────────────────────────────────────────

    async def invalidate(self, key: str, reason: str = "explicit"):
        self._local.pop(key, None)
        if self.redis:
            await self.redis.delete(f"{REDIS_PREFIX}{key}")
            await self.redis.publish(
                REDIS_CHANNEL,
                json.dumps({"key": key, "reason": reason, "ts": time.time()}),
            )
        log.debug(f"Invalidated: {key} ({reason})")
        for cb in self._on_invalidate:
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(key, reason)
                else:
                    cb(key, reason)
            except Exception as e:
                log.warning(f"Invalidate callback error: {e}")

    async def invalidate_tag(self, tag: str, reason: str = "tag") -> int:
        """Invalidate all keys with a given tag."""
        keys_local = self._tag_map.get(tag, set())
        keys_redis: set[str] = set()

        if self.redis:
            redis_keys = await self.redis.smembers(f"{REDIS_DEPS}:tag:{tag}")
            keys_redis = set(redis_keys)

        all_keys = keys_local | keys_redis
        for key in all_keys:
            await self.invalidate(key, reason=f"tag:{tag}")

        # Clean up tag mapping
        self._tag_map.pop(tag, None)
        if self.redis:
            await self.redis.delete(f"{REDIS_DEPS}:tag:{tag}")

        log.info(f"Invalidated tag '{tag}': {len(all_keys)} keys")
        return len(all_keys)

    async def invalidate_pattern(self, pattern: str) -> int:
        """Invalidate all keys matching a glob pattern."""
        count = 0
        # Local
        to_del = [k for k in self._local if _glob_match(pattern, k)]
        for key in to_del:
            await self.invalidate(key, reason=f"pattern:{pattern}")
            count += 1

        # Redis
        if self.redis:
            full_pattern = f"{REDIS_PREFIX}{pattern}"
            async for redis_key in self.redis.scan_iter(full_pattern):
                key = redis_key.replace(REDIS_PREFIX, "", 1)
                if key not in [k.replace(REDIS_PREFIX, "") for k in to_del]:
                    await self.invalidate(key, reason=f"pattern:{pattern}")
                    count += 1

        log.info(f"Invalidated pattern '{pattern}': {count} keys")
        return count

    async def flush_expired(self) -> int:
        """Remove expired entries from local cache."""
        expired = [k for k, e in self._local.items() if e.expired]
        for key in expired:
            del self._local[key]
        if expired:
            log.debug(f"Flushed {len(expired)} expired local entries")
        return len(expired)

    async def flush_all(self):
        self._local.clear()
        self._tag_map.clear()
        if self.redis:
            async for key in self.redis.scan_iter(f"{REDIS_PREFIX}*"):
                await self.redis.delete(key)
        log.warning("Full cache flush executed")

    # ── Stats ──────────────────────────────────────────────────────────────────

    async def stats(self) -> dict:
        local_count = len(self._local)
        local_expired = sum(1 for e in self._local.values() if e.expired)
        total_hits = sum(e.hits for e in self._local.values())
        redis_count = 0
        if self.redis:
            async for _ in self.redis.scan_iter(f"{REDIS_PREFIX}*"):
                redis_count += 1
        return {
            "local_entries": local_count,
            "local_expired": local_expired,
            "local_valid": local_count - local_expired,
            "redis_entries": redis_count,
            "total_hits": total_hits,
            "tags": len(self._tag_map),
            "tagged_keys": sum(len(v) for v in self._tag_map.values()),
        }

    def hot_keys(self, limit: int = 10) -> list[dict]:
        return sorted(
            [
                {
                    "key": k,
                    "hits": e.hits,
                    "ttl_remaining": max(0, e.ttl_s - (time.time() - e.created_at)),
                }
                for k, e in self._local.items()
                if not e.expired
            ],
            key=lambda x: -x["hits"],
        )[:limit]


def _glob_match(pattern: str, text: str) -> bool:
    import fnmatch

    return fnmatch.fnmatch(text, pattern)


async def main():
    import sys

    cache = CacheInvalidator()
    await cache.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        # Set entries with tags
        await cache.set(
            "model:qwen3.5-9b:status", "loaded", ttl_s=60, tags=["model", "M1"]
        )
        await cache.set(
            "model:qwen3.5-35b:status", "idle", ttl_s=60, tags=["model", "M1"]
        )
        await cache.set("user:turbo:session", "active", ttl_s=300, tags=["user"])
        await cache.set("gpu:0:temp", "62", ttl_s=10, tags=["gpu", "M1"])

        # Read
        val = await cache.get("model:qwen3.5-9b:status")
        print(f"model status: {val}")

        # Invalidate by tag (all M1 entries)
        count = await cache.invalidate_tag("M1")
        print(f"Invalidated tag 'M1': {count} keys")

        # Should be None now
        val = await cache.get("model:qwen3.5-9b:status")
        print(f"After invalidation: {val}")

        s = await cache.stats()
        print(f"\nStats: {s}")

    elif cmd == "stats":
        s = await cache.stats()
        print(json.dumps(s, indent=2))

    elif cmd == "invalidate-tag" and len(sys.argv) > 2:
        count = await cache.invalidate_tag(sys.argv[2])
        print(f"Invalidated {count} keys for tag '{sys.argv[2]}'")

    elif cmd == "hot":
        for entry in cache.hot_keys():
            print(
                f"  {entry['key']}: {entry['hits']} hits, {entry['ttl_remaining']:.0f}s remaining"
            )

    elif cmd == "flush":
        await cache.flush_all()
        print("Cache flushed")


if __name__ == "__main__":
    asyncio.run(main())
