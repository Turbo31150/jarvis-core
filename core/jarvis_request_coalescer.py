#!/usr/bin/env python3
"""
jarvis_request_coalescer — Deduplicates in-flight identical requests (request coalescing)
When multiple callers request the same key concurrently, only one upstream call is made
"""

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.request_coalescer")

REDIS_PREFIX = "jarvis:coalesce:"


@dataclass
class CoalescedCall:
    key: str
    waiters: list[asyncio.Future] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    result: Any = None
    error: Exception | None = None
    done: bool = False

    @property
    def wait_count(self) -> int:
        return len(self.waiters)

    @property
    def age_ms(self) -> float:
        return (time.time() - self.started_at) * 1000


@dataclass
class CoalesceStats:
    key: str
    total_calls: int
    coalesced_calls: int
    last_duration_ms: float

    @property
    def coalesce_ratio(self) -> float:
        return self.coalesced_calls / max(self.total_calls, 1)


class RequestCoalescer:
    """
    Coalesces concurrent identical requests into a single upstream call.
    Subsequent callers with the same key wait for the first call to complete
    and share its result.
    """

    def __init__(self, ttl_s: float = 0.0):
        """
        ttl_s: if > 0, cache successful results for this duration (like a short-lived cache).
               if 0, only coalesce truly concurrent requests.
        """
        self.redis: aioredis.Redis | None = None
        self._inflight: dict[str, CoalescedCall] = {}
        self._result_cache: dict[
            str, tuple[Any, float]
        ] = {}  # key → (result, expire_ts)
        self._ttl_s = ttl_s
        self._per_key_stats: dict[str, CoalesceStats] = {}
        self._stats: dict[str, int] = {
            "total_requests": 0,
            "upstream_calls": 0,
            "coalesced": 0,
            "cache_hits": 0,
            "errors": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    @staticmethod
    def make_key(fn_name: str, *args, **kwargs) -> str:
        raw = json.dumps(
            {"fn": fn_name, "args": args, "kwargs": kwargs}, sort_keys=True
        )
        return hashlib.md5(raw.encode()).hexdigest()

    async def call(
        self,
        key: str,
        fn: Callable,
        *args,
        **kwargs,
    ) -> Any:
        """
        Call fn(*args, **kwargs) but coalesce concurrent calls with the same key.
        Returns the result (possibly shared with other waiters).
        """
        self._stats["total_requests"] += 1

        # Check short-lived result cache
        if self._ttl_s > 0:
            cached = self._result_cache.get(key)
            if cached and time.time() < cached[1]:
                self._stats["cache_hits"] += 1
                return cached[0]
            # Prune expired entries
            self._result_cache = {
                k: v for k, v in self._result_cache.items() if time.time() < v[1]
            }

        # Check if already inflight
        call = self._inflight.get(key)
        if call and not call.done:
            # Coalesce — wait for the inflight call
            self._stats["coalesced"] += 1
            self._update_per_key(key, coalesced=True)
            loop = asyncio.get_event_loop()
            fut: asyncio.Future = loop.create_future()
            call.waiters.append(fut)
            try:
                return await fut
            except Exception:
                raise

        # Start a new upstream call
        self._stats["upstream_calls"] += 1
        self._update_per_key(key, coalesced=False)
        call = CoalescedCall(key=key)
        self._inflight[key] = call

        try:
            result = await fn(*args, **kwargs)
            call.result = result
            call.done = True

            # Cache result if TTL configured
            if self._ttl_s > 0:
                self._result_cache[key] = (result, time.time() + self._ttl_s)

            # Resolve all waiters
            for fut in call.waiters:
                if not fut.done():
                    fut.set_result(result)

            return result

        except Exception as e:
            self._stats["errors"] += 1
            call.error = e
            call.done = True
            # Propagate error to all waiters
            for fut in call.waiters:
                if not fut.done():
                    fut.set_exception(e)
            raise

        finally:
            # Remove from inflight after all waiters got the result
            self._inflight.pop(key, None)

    def _update_per_key(self, key: str, coalesced: bool):
        if key not in self._per_key_stats:
            self._per_key_stats[key] = CoalesceStats(key, 0, 0, 0.0)
        s = self._per_key_stats[key]
        s.total_calls += 1
        if coalesced:
            s.coalesced_calls += 1

    def inflight_keys(self) -> list[str]:
        return [k for k, c in self._inflight.items() if not c.done]

    def inflight_details(self) -> list[dict]:
        return [
            {"key": k[:32], "waiters": c.wait_count, "age_ms": round(c.age_ms, 1)}
            for k, c in self._inflight.items()
            if not c.done
        ]

    def top_coalesced_keys(self, n: int = 10) -> list[dict]:
        sorted_keys = sorted(
            self._per_key_stats.values(),
            key=lambda s: -s.coalesced_calls,
        )
        return [
            {
                "key": s.key[:32],
                "total_calls": s.total_calls,
                "coalesced": s.coalesced_calls,
                "ratio": round(s.coalesce_ratio, 3),
            }
            for s in sorted_keys[:n]
        ]

    def stats(self) -> dict:
        total = self._stats["total_requests"]
        coalesced = self._stats["coalesced"]
        return {
            **self._stats,
            "coalesce_ratio": round(coalesced / max(total, 1), 3),
            "inflight": len(self.inflight_keys()),
            "cached_keys": len(self._result_cache),
        }


def build_jarvis_coalescer(ttl_s: float = 2.0) -> RequestCoalescer:
    return RequestCoalescer(ttl_s=ttl_s)


async def main():
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        coalescer = build_jarvis_coalescer(ttl_s=1.0)
        call_count = 0

        async def expensive_llm_call(prompt: str) -> str:
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.1)  # simulate 100ms inference
            return f"Response to: {prompt[:30]}"

        prompt = "What is the capital of France?"
        key = RequestCoalescer.make_key("llm", prompt)

        print("Launching 10 concurrent requests for the same prompt...")
        tasks = [coalescer.call(key, expensive_llm_call, prompt) for _ in range(10)]
        results = await asyncio.gather(*tasks)
        print(f"  Upstream calls made: {call_count} (expected: 1)")
        print(f"  All got same result: {len(set(results)) == 1}")
        print(f"  Result: {results[0]}")

        # Second batch — should hit cache
        call_count = 0
        print("\nLaunching 5 more requests (should hit cache)...")
        tasks2 = [coalescer.call(key, expensive_llm_call, prompt) for _ in range(5)]
        results2 = await asyncio.gather(*tasks2)
        print(f"  Upstream calls made: {call_count} (expected: 0 — cache hit)")

        # Different prompt — new upstream call
        key2 = RequestCoalescer.make_key("llm", "Different question")
        await coalescer.call(key2, expensive_llm_call, "Different question")

        print("\nTop coalesced keys:")
        for k in coalescer.top_coalesced_keys():
            print(
                f"  {k['key'][:24]}  total={k['total_calls']} coalesced={k['coalesced']} ratio={k['ratio']:.2%}"
            )

        print(f"\nStats: {json.dumps(coalescer.stats(), indent=2)}")

    elif cmd == "stats":
        coalescer = build_jarvis_coalescer()
        print(json.dumps(coalescer.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
