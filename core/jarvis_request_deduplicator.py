#!/usr/bin/env python3
"""
jarvis_request_deduplicator — In-flight request deduplication
Merges identical concurrent requests to avoid redundant LLM calls
"""

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.request_deduplicator")

REDIS_PREFIX = "jarvis:dedup:"
DEFAULT_TTL_S = 30.0


def _request_key(payload: Any) -> str:
    try:
        raw = json.dumps(payload, sort_keys=True, default=str)
    except Exception:
        raw = str(payload)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class PendingRequest:
    key: str
    future: asyncio.Future
    created_at: float = field(default_factory=time.time)
    waiters: int = 1


@dataclass
class DeduplicationStats:
    total_requests: int = 0
    deduplicated: int = 0
    cache_hits: int = 0
    errors: int = 0

    @property
    def dedup_rate(self) -> float:
        return round(self.deduplicated / max(self.total_requests, 1), 3)

    def to_dict(self) -> dict:
        return {
            "total": self.total_requests,
            "deduplicated": self.deduplicated,
            "cache_hits": self.cache_hits,
            "errors": self.errors,
            "dedup_rate": self.dedup_rate,
        }


class RequestDeduplicator:
    def __init__(self, ttl_s: float = DEFAULT_TTL_S):
        self.redis: aioredis.Redis | None = None
        self.ttl_s = ttl_s
        self._pending: dict[str, PendingRequest] = {}
        self._result_cache: dict[
            str, tuple[Any, float]
        ] = {}  # key → (result, expires_at)
        self._stats = DeduplicationStats()

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    async def execute(
        self,
        payload: Any,
        fn: Callable,
        *args: Any,
        cache_ttl_s: float = 0.0,
        **kwargs: Any,
    ) -> Any:
        """
        Execute fn(*args, **kwargs) once for identical concurrent payloads.
        If cache_ttl_s > 0, also cache results for that duration.
        """
        key = _request_key(payload)
        self._stats.total_requests += 1

        # Check local result cache
        if cache_ttl_s > 0:
            cached = self._result_cache.get(key)
            if cached:
                result, expires = cached
                if time.time() < expires:
                    self._stats.cache_hits += 1
                    log.debug(f"Cache hit: {key}")
                    return result
                else:
                    del self._result_cache[key]

        # Check Redis cache
        if cache_ttl_s > 0 and self.redis:
            raw = await self.redis.get(f"{REDIS_PREFIX}cache:{key}")
            if raw:
                self._stats.cache_hits += 1
                return json.loads(raw)

        # Check in-flight deduplication
        if key in self._pending:
            pending = self._pending[key]
            pending.waiters += 1
            self._stats.deduplicated += 1
            log.debug(f"Dedup hit: {key} (waiters={pending.waiters})")
            try:
                return await asyncio.shield(pending.future)
            except Exception:
                raise

        # New request — create future and execute
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[key] = PendingRequest(key=key, future=future)

        try:
            if asyncio.iscoroutinefunction(fn):
                result = await fn(*args, **kwargs)
            else:
                result = fn(*args, **kwargs)

            future.set_result(result)

            # Store in cache
            if cache_ttl_s > 0:
                self._result_cache[key] = (result, time.time() + cache_ttl_s)
                if self.redis:
                    try:
                        await self.redis.setex(
                            f"{REDIS_PREFIX}cache:{key}",
                            int(cache_ttl_s),
                            json.dumps(result, default=str),
                        )
                    except Exception:
                        pass

            return result

        except Exception as e:
            self._stats.errors += 1
            if not future.done():
                future.set_exception(e)
            raise

        finally:
            self._pending.pop(key, None)

    async def invalidate(self, payload: Any):
        """Remove cached result for a payload."""
        key = _request_key(payload)
        self._result_cache.pop(key, None)
        if self.redis:
            await self.redis.delete(f"{REDIS_PREFIX}cache:{key}")

    def pending_count(self) -> int:
        return len(self._pending)

    def cache_size(self) -> int:
        # Prune expired
        now = time.time()
        expired = [k for k, (_, exp) in self._result_cache.items() if exp < now]
        for k in expired:
            del self._result_cache[k]
        return len(self._result_cache)

    def stats(self) -> dict:
        return {
            **self._stats.to_dict(),
            "in_flight": self.pending_count(),
            "cache_size": self.cache_size(),
        }


async def main():
    import sys

    dedup = RequestDeduplicator(ttl_s=5.0)
    await dedup.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        call_count = 0

        async def expensive_call(prompt: str) -> str:
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.3)
            return f"result for '{prompt}' (call #{call_count})"

        # Launch 5 identical concurrent requests
        payload = {"prompt": "What is Redis?", "model": "qwen3.5"}
        tasks = [
            dedup.execute(payload, expensive_call, payload["prompt"], cache_ttl_s=10.0)
            for _ in range(5)
        ]
        results = await asyncio.gather(*tasks)
        print(f"5 concurrent identical requests → {call_count} actual call(s)")
        for i, r in enumerate(results):
            print(f"  [{i}] {r}")

        # Second wave — should hit cache
        r = await dedup.execute(
            payload, expensive_call, payload["prompt"], cache_ttl_s=10.0
        )
        print(f"\nCached result: {r}")
        print(f"\nStats: {json.dumps(dedup.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(dedup.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
