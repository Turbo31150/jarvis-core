#!/usr/bin/env python3
"""
jarvis_api_throttler — Multi-dimensional API rate limiting
Per-user, per-model, per-endpoint rate limiting with sliding window and burst
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.api_throttler")

REDIS_PREFIX = "jarvis:throttle:"


@dataclass
class RateLimit:
    key: str  # e.g. "user:turbo", "model:qwen3.5-9b", "endpoint:/infer"
    rpm: int  # requests per minute
    rph: int = 0  # requests per hour (0 = unlimited)
    burst: int = 0  # extra burst allowance (0 = same as rpm)
    tokens_per_min: int = 0  # token rate limit (0 = unlimited)

    @property
    def effective_burst(self) -> int:
        return self.burst or self.rpm


@dataclass
class ThrottleDecision:
    allowed: bool
    key: str
    reason: str = ""
    retry_after_s: float = 0.0
    current_rpm: float = 0.0
    limit_rpm: int = 0
    remaining: int = 0

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "key": self.key,
            "reason": self.reason,
            "retry_after_s": round(self.retry_after_s, 2),
            "current_rpm": round(self.current_rpm, 1),
            "limit_rpm": self.limit_rpm,
            "remaining": self.remaining,
        }


class SlidingWindowCounter:
    """In-memory sliding window using bucketed timestamps."""

    def __init__(self, window_s: float = 60.0, buckets: int = 60):
        self.window_s = window_s
        self.bucket_s = window_s / buckets
        self._buckets: dict[int, int] = {}  # bucket_ts → count

    def _bucket_key(self, ts: float) -> int:
        return int(ts / self.bucket_s) * int(self.bucket_s)

    def _prune(self, now: float):
        cutoff = now - self.window_s
        stale = [k for k in self._buckets if k < cutoff]
        for k in stale:
            del self._buckets[k]

    def increment(self, ts: float | None = None, amount: int = 1) -> int:
        now = ts or time.time()
        self._prune(now)
        bk = self._bucket_key(now)
        self._buckets[bk] = self._buckets.get(bk, 0) + amount
        return self.count(now)

    def count(self, ts: float | None = None) -> int:
        now = ts or time.time()
        self._prune(now)
        return sum(self._buckets.values())


class TokenBucket:
    """Token bucket for burst handling."""

    def __init__(self, capacity: int, refill_rate: float):
        self.capacity = capacity
        self.refill_rate = refill_rate  # tokens per second
        self._tokens = float(capacity)
        self._last_refill = time.time()

    def consume(self, amount: int = 1) -> bool:
        now = time.time()
        elapsed = now - self._last_refill
        self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_rate)
        self._last_refill = now
        if self._tokens >= amount:
            self._tokens -= amount
            return True
        return False

    @property
    def available(self) -> int:
        return int(self._tokens)

    def retry_after_s(self) -> float:
        deficit = 1 - self._tokens
        return max(0.0, deficit / self.refill_rate)


class APIThrottler:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._limits: dict[str, RateLimit] = {}
        self._windows: dict[str, SlidingWindowCounter] = {}
        self._hourly_windows: dict[str, SlidingWindowCounter] = {}
        self._buckets: dict[str, TokenBucket] = {}
        self._token_windows: dict[str, SlidingWindowCounter] = {}
        self._stats: dict[str, int] = {"allowed": 0, "throttled": 0, "checks": 0}

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def set_limit(self, limit: RateLimit):
        self._limits[limit.key] = limit
        self._windows[limit.key] = SlidingWindowCounter(window_s=60.0)
        if limit.rph > 0:
            self._hourly_windows[limit.key] = SlidingWindowCounter(
                window_s=3600.0, buckets=60
            )
        burst = limit.effective_burst
        self._buckets[limit.key] = TokenBucket(
            capacity=burst, refill_rate=limit.rpm / 60.0
        )
        if limit.tokens_per_min > 0:
            self._token_windows[limit.key] = SlidingWindowCounter(window_s=60.0)

    def check(self, key: str, tokens: int = 0) -> ThrottleDecision:
        self._stats["checks"] += 1
        limit = self._limits.get(key)
        if not limit:
            self._stats["allowed"] += 1
            return ThrottleDecision(allowed=True, key=key, reason="no_limit")

        # Burst bucket check
        bucket = self._buckets[key]
        if not bucket.consume(1):
            self._stats["throttled"] += 1
            retry = bucket.retry_after_s()
            return ThrottleDecision(
                allowed=False,
                key=key,
                reason="burst_exceeded",
                retry_after_s=retry,
                current_rpm=self._windows[key].count(),
                limit_rpm=limit.rpm,
                remaining=0,
            )

        # Sliding window RPM check
        window = self._windows[key]
        current = window.increment()
        remaining = max(0, limit.rpm - current)

        if current > limit.rpm:
            self._stats["throttled"] += 1
            return ThrottleDecision(
                allowed=False,
                key=key,
                reason="rpm_exceeded",
                retry_after_s=1.0,
                current_rpm=current,
                limit_rpm=limit.rpm,
                remaining=0,
            )

        # Hourly check
        if limit.rph > 0:
            hw = self._hourly_windows[key]
            hourly = hw.increment()
            if hourly > limit.rph:
                self._stats["throttled"] += 1
                return ThrottleDecision(
                    allowed=False,
                    key=key,
                    reason="rph_exceeded",
                    retry_after_s=60.0,
                    current_rpm=current,
                    limit_rpm=limit.rpm,
                    remaining=0,
                )

        # Token rate check
        if tokens > 0 and limit.tokens_per_min > 0:
            tw = self._token_windows[key]
            current_tokens = tw.increment(amount=tokens)
            if current_tokens > limit.tokens_per_min:
                self._stats["throttled"] += 1
                return ThrottleDecision(
                    allowed=False,
                    key=key,
                    reason="token_rate_exceeded",
                    retry_after_s=5.0,
                    current_rpm=current,
                    limit_rpm=limit.rpm,
                    remaining=remaining,
                )

        self._stats["allowed"] += 1
        return ThrottleDecision(
            allowed=True,
            key=key,
            current_rpm=current,
            limit_rpm=limit.rpm,
            remaining=remaining,
        )

    def check_multi(self, keys: list[str], tokens: int = 0) -> ThrottleDecision:
        """Check multiple keys (user + model + endpoint), return first denial."""
        for key in keys:
            decision = self.check(key, tokens=tokens)
            if not decision.allowed:
                return decision
        # All passed — return last (most specific) decision
        return self.check(keys[-1]) if keys else ThrottleDecision(allowed=True, key="")

    def remaining(self, key: str) -> int:
        limit = self._limits.get(key)
        if not limit:
            return 999999
        current = self._windows.get(key, SlidingWindowCounter()).count()
        return max(0, limit.rpm - current)

    def stats(self) -> dict:
        return {
            **self._stats,
            "limits_configured": len(self._limits),
            "throttle_rate": round(
                self._stats["throttled"] / max(self._stats["checks"], 1) * 100, 1
            ),
        }


def build_jarvis_throttler() -> APIThrottler:
    t = APIThrottler()
    t.set_limit(RateLimit("user:default", rpm=60, rph=1000, burst=10))
    t.set_limit(RateLimit("user:turbo", rpm=300, rph=10000, burst=30))
    t.set_limit(
        RateLimit(
            "model:claude-opus-4", rpm=10, rph=100, burst=3, tokens_per_min=100000
        )
    )
    t.set_limit(RateLimit("model:qwen3.5-9b", rpm=200, burst=50))
    t.set_limit(RateLimit("endpoint:/infer", rpm=500, burst=100))
    return t


async def main():
    import sys

    throttler = build_jarvis_throttler()
    await throttler.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Simulating 70 requests from user:default (limit=60rpm):")
        allowed = 0
        throttled = 0
        for _ in range(70):
            d = throttler.check("user:default")
            if d.allowed:
                allowed += 1
            else:
                throttled += 1
        print(f"  Allowed: {allowed}, Throttled: {throttled}")

        print("\nMulti-key check (user:turbo + model:claude-opus-4):")
        for i in range(15):
            d = throttler.check_multi(["user:turbo", "model:claude-opus-4"])
            status = "✅" if d.allowed else f"❌ {d.reason}"
            print(f"  Request {i + 1:>2}: {status}")

        print(f"\nStats: {json.dumps(throttler.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(throttler.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
