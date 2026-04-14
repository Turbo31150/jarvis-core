#!/usr/bin/env python3
"""
jarvis_request_dedup — In-flight request deduplication
Collapses identical concurrent requests into a single backend call
"""

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.request_dedup")

REDIS_PREFIX = "jarvis:dedup:"
DEDUP_TTL = 10  # seconds — max wait for in-flight result


def _request_key(
    model: str, messages: list, max_tokens: int, temperature: float
) -> str:
    payload = json.dumps(
        {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


@dataclass
class InFlight:
    key: str
    future: asyncio.Future
    started_at: float = field(default_factory=time.time)
    waiters: int = 1


class RequestDedup:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._inflight: dict[str, InFlight] = {}
        self._stats = {"hits": 0, "misses": 0, "saved_calls": 0}

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    async def execute(
        self,
        key: str,
        coro_factory,
    ):
        """
        Execute coro_factory() only once per key, even if called concurrently.
        All concurrent callers with the same key get the same result.
        """
        if key in self._inflight:
            entry = self._inflight[key]
            entry.waiters += 1
            self._stats["hits"] += 1
            self._stats["saved_calls"] += 1
            log.debug(f"Dedup HIT {key} (waiter #{entry.waiters})")
            return await asyncio.shield(entry.future)

        self._stats["misses"] += 1
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._inflight[key] = InFlight(key=key, future=future)

        try:
            result = await coro_factory()
            future.set_result(result)
            return result
        except Exception as e:
            future.set_exception(e)
            raise
        finally:
            self._inflight.pop(key, None)

    def stats(self) -> dict:
        return {
            **self._stats,
            "inflight": len(self._inflight),
            "hit_rate_pct": round(
                self._stats["hits"]
                / max(self._stats["hits"] + self._stats["misses"], 1)
                * 100,
                1,
            ),
        }

    def inflight_list(self) -> list[dict]:
        return [
            {
                "key": e.key,
                "waiters": e.waiters,
                "age_ms": round((time.time() - e.started_at) * 1000, 1),
            }
            for e in self._inflight.values()
        ]


async def main():
    import sys

    dedup = RequestDedup()
    await dedup.connect_redis()

    if len(sys.argv) > 1 and sys.argv[1] == "demo":

        async def fake_llm_call(n: int):
            await asyncio.sleep(0.5)
            return f"response_{n}"

        key = "same_request_key"
        tasks = [dedup.execute(key, lambda: fake_llm_call(1)) for _ in range(5)]
        results = await asyncio.gather(*tasks)
        print(f"5 concurrent calls → {len(set(results))} unique result: {results[0]}")
        print(json.dumps(dedup.stats(), indent=2))
    else:
        print(json.dumps(dedup.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
