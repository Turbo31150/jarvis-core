#!/usr/bin/env python3
"""
jarvis_rate_limiter_v2 — Per-client/per-model sliding window rate limiting
Redis-backed, supports burst allowance, per-tier quotas, and backpressure signaling
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.rate_limiter_v2")

REDIS_PREFIX = "jarvis:rl:"
DEFAULT_WINDOW_S = 60
DEFAULT_LIMIT = 20


@dataclass
class RateTier:
    name: str
    requests_per_minute: int
    requests_per_hour: int
    burst: int = 5  # extra requests allowed in short bursts


TIERS: dict[str, RateTier] = {
    "default": RateTier("default", 20, 200, burst=5),
    "premium": RateTier("premium", 60, 600, burst=15),
    "internal": RateTier("internal", 200, 2000, burst=50),
    "restricted": RateTier("restricted", 5, 30, burst=2),
}


@dataclass
class RateLimitResult:
    allowed: bool
    client_id: str
    tier: str
    remaining_minute: int
    remaining_hour: int
    reset_at: float
    retry_after: float = 0.0

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "client_id": self.client_id,
            "tier": self.tier,
            "remaining_minute": self.remaining_minute,
            "remaining_hour": self.remaining_hour,
            "reset_at": self.reset_at,
            "retry_after": self.retry_after,
        }


class RateLimiterV2:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._client_tiers: dict[str, str] = {}
        self._local_counts: dict[str, list[float]] = {}  # fallback if no Redis

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def set_tier(self, client_id: str, tier: str):
        if tier in TIERS:
            self._client_tiers[client_id] = tier

    def _get_tier(self, client_id: str) -> RateTier:
        tier_name = self._client_tiers.get(client_id, "default")
        return TIERS.get(tier_name, TIERS["default"])

    async def check(
        self,
        client_id: str,
        cost: int = 1,
        model: str | None = None,
    ) -> RateLimitResult:
        """Check and consume rate limit quota. Returns allowed/denied."""
        tier = self._get_tier(client_id)
        key_prefix = f"{REDIS_PREFIX}{client_id}"
        if model:
            key_prefix += f":{model}"

        now = time.time()

        if self.redis:
            return await self._check_redis(client_id, tier, key_prefix, now, cost)
        else:
            return self._check_local(client_id, tier, now, cost)

    async def _check_redis(
        self,
        client_id: str,
        tier: RateTier,
        key_prefix: str,
        now: float,
        cost: int,
    ) -> RateLimitResult:
        key_min = f"{key_prefix}:min"
        key_hour = f"{key_prefix}:hour"
        window_start_min = now - 60
        window_start_hour = now - 3600

        # Sliding window: remove old entries, count remaining
        pipe = self.redis.pipeline()
        pipe.zremrangebyscore(key_min, 0, window_start_min)
        pipe.zremrangebyscore(key_hour, 0, window_start_hour)
        pipe.zcard(key_min)
        pipe.zcard(key_hour)
        results = await pipe.execute()

        count_min = results[2]
        count_hour = results[3]

        limit_min = tier.requests_per_minute + tier.burst
        limit_hour = tier.requests_per_hour

        if count_min + cost > limit_min or count_hour + cost > limit_hour:
            # Rate limited
            # Find when next slot opens
            oldest_min = await self.redis.zrange(key_min, 0, 0, withscores=True)
            retry_after = 0.0
            reset_at = now + 60
            if oldest_min:
                reset_at = oldest_min[0][1] + 60
                retry_after = max(0.0, reset_at - now)

            log.warning(
                f"Rate limited: {client_id} min={count_min}/{limit_min} hour={count_hour}/{limit_hour}"
            )
            return RateLimitResult(
                allowed=False,
                client_id=client_id,
                tier=tier.name,
                remaining_minute=max(0, limit_min - count_min),
                remaining_hour=max(0, limit_hour - count_hour),
                reset_at=reset_at,
                retry_after=retry_after,
            )

        # Allow: record request
        ts_member = f"{now:.6f}"
        pipe = self.redis.pipeline()
        for _ in range(cost):
            pipe.zadd(key_min, {ts_member: now})
            pipe.zadd(key_hour, {ts_member: now})
        pipe.expire(key_min, 120)
        pipe.expire(key_hour, 7200)
        await pipe.execute()

        return RateLimitResult(
            allowed=True,
            client_id=client_id,
            tier=tier.name,
            remaining_minute=limit_min - count_min - cost,
            remaining_hour=limit_hour - count_hour - cost,
            reset_at=now + 60,
        )

    def _check_local(
        self,
        client_id: str,
        tier: RateTier,
        now: float,
        cost: int,
    ) -> RateLimitResult:
        """In-memory fallback when Redis unavailable."""
        key = client_id
        if key not in self._local_counts:
            self._local_counts[key] = []

        # Prune old entries
        self._local_counts[key] = [t for t in self._local_counts[key] if now - t < 3600]
        timestamps = self._local_counts[key]

        count_min = sum(1 for t in timestamps if now - t < 60)
        count_hour = len(timestamps)
        limit_min = tier.requests_per_minute + tier.burst
        limit_hour = tier.requests_per_hour

        if count_min + cost > limit_min or count_hour + cost > limit_hour:
            return RateLimitResult(
                allowed=False,
                client_id=client_id,
                tier=tier.name,
                remaining_minute=max(0, limit_min - count_min),
                remaining_hour=max(0, limit_hour - count_hour),
                reset_at=now + 60,
                retry_after=60.0,
            )

        for _ in range(cost):
            self._local_counts[key].append(now)

        return RateLimitResult(
            allowed=True,
            client_id=client_id,
            tier=tier.name,
            remaining_minute=limit_min - count_min - cost,
            remaining_hour=limit_hour - count_hour - cost,
            reset_at=now + 60,
        )

    async def get_stats(self, client_id: str | None = None) -> list[dict]:
        """Return usage stats per client."""
        if not self.redis:
            return []

        pattern = f"{REDIS_PREFIX}*:min"
        keys = []
        async for key in self.redis.scan_iter(pattern):
            keys.append(key)

        now = time.time()
        stats = []
        for key in keys:
            parts = key.replace(REDIS_PREFIX, "").replace(":min", "").split(":")
            cid = parts[0]
            if client_id and cid != client_id:
                continue
            count_min = await self.redis.zcount(key, now - 60, now)
            tier = self._get_tier(cid)
            stats.append(
                {
                    "client_id": cid,
                    "tier": tier.name,
                    "requests_last_min": count_min,
                    "limit_per_min": tier.requests_per_minute,
                    "utilization_pct": round(
                        count_min / tier.requests_per_minute * 100, 1
                    ),
                }
            )

        return sorted(stats, key=lambda x: -x["utilization_pct"])

    async def reset(self, client_id: str):
        """Reset all rate limit counters for a client."""
        if not self.redis:
            self._local_counts.pop(client_id, None)
            return
        pattern = f"{REDIS_PREFIX}{client_id}*"
        async for key in self.redis.scan_iter(pattern):
            await self.redis.delete(key)
        log.info(f"Reset rate limits for {client_id}")


async def main():
    import sys

    limiter = RateLimiterV2()
    await limiter.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        limiter.set_tier("test_client", "default")
        print("Sending 25 requests as 'test_client' (limit: 20/min)...")
        for i in range(25):
            r = await limiter.check("test_client")
            status = "✅" if r.allowed else f"❌ retry in {r.retry_after:.1f}s"
            print(f"  [{i + 1:2d}] {status}  remaining={r.remaining_minute}/min")
            if not r.allowed:
                break

    elif cmd == "check" and len(sys.argv) > 2:
        client_id = sys.argv[2]
        r = await limiter.check(client_id)
        print(json.dumps(r.to_dict(), indent=2))

    elif cmd == "stats":
        stats = await limiter.get_stats()
        if not stats:
            print("No data")
        else:
            print(
                f"{'Client':<20} {'Tier':<12} {'Req/min':>8} {'Limit':>6} {'Util%':>7}"
            )
            print("-" * 55)
            for s in stats:
                print(
                    f"{s['client_id']:<20} {s['tier']:<12} {s['requests_last_min']:>8} {s['limit_per_min']:>6} {s['utilization_pct']:>6.1f}%"
                )

    elif cmd == "tier" and len(sys.argv) > 3:
        limiter.set_tier(sys.argv[2], sys.argv[3])
        print(f"Set {sys.argv[2]} → tier:{sys.argv[3]}")

    elif cmd == "reset" and len(sys.argv) > 2:
        await limiter.reset(sys.argv[2])
        print(f"Reset: {sys.argv[2]}")


if __name__ == "__main__":
    asyncio.run(main())
