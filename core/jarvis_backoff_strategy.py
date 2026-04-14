#!/usr/bin/env python3
"""
jarvis_backoff_strategy — Pluggable backoff strategies for rate limiting and retry
Provides adaptive, token-bucket, and deadline-aware backoff implementations
"""

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import AsyncIterator


log = logging.getLogger("jarvis.backoff_strategy")

REDIS_PREFIX = "jarvis:backoff:"


@dataclass
class BackoffState:
    name: str
    attempts: int = 0
    total_wait_s: float = 0.0
    last_wait_s: float = 0.0
    created_at: float = field(default_factory=time.time)
    deadline: float = 0.0  # 0 = no deadline

    @property
    def elapsed_s(self) -> float:
        return time.time() - self.created_at

    @property
    def deadline_remaining_s(self) -> float:
        if not self.deadline:
            return float("inf")
        return max(0.0, self.deadline - time.time())

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "attempts": self.attempts,
            "total_wait_s": round(self.total_wait_s, 2),
            "last_wait_s": round(self.last_wait_s, 2),
            "elapsed_s": round(self.elapsed_s, 2),
        }


class BaseBackoff:
    """Base class for backoff strategies."""

    def __init__(self, name: str = "default", deadline_s: float = 0.0):
        self.state = BackoffState(
            name=name,
            deadline=time.time() + deadline_s if deadline_s else 0.0,
        )

    def reset(self):
        self.state.attempts = 0
        self.state.total_wait_s = 0.0
        self.state.last_wait_s = 0.0

    def compute(self) -> float:
        raise NotImplementedError

    async def wait(self) -> bool:
        """Wait for next retry interval. Returns False if deadline exceeded."""
        if self.state.deadline and time.time() >= self.state.deadline:
            return False
        delay = self.compute()
        remaining = self.state.deadline_remaining_s
        actual = min(delay, remaining) if self.state.deadline else delay
        if actual <= 0:
            return False
        self.state.attempts += 1
        self.state.last_wait_s = actual
        self.state.total_wait_s += actual
        log.debug(
            f"Backoff [{self.state.name}] attempt={self.state.attempts} wait={actual:.2f}s"
        )
        await asyncio.sleep(actual)
        return True

    async def attempts(self, max_attempts: int = 10) -> AsyncIterator[int]:
        """Async generator for retry loops."""
        for i in range(max_attempts):
            yield i
            if i < max_attempts - 1:
                ok = await self.wait()
                if not ok:
                    break


class FixedBackoff(BaseBackoff):
    def __init__(self, delay_s: float = 1.0, **kwargs):
        super().__init__(**kwargs)
        self.delay_s = delay_s

    def compute(self) -> float:
        return self.delay_s


class LinearBackoff(BaseBackoff):
    def __init__(
        self,
        base_s: float = 1.0,
        increment_s: float = 1.0,
        max_s: float = 30.0,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.base_s = base_s
        self.increment_s = increment_s
        self.max_s = max_s

    def compute(self) -> float:
        return min(self.base_s + self.increment_s * self.state.attempts, self.max_s)


class ExponentialBackoff(BaseBackoff):
    def __init__(
        self,
        base_s: float = 1.0,
        multiplier: float = 2.0,
        max_s: float = 60.0,
        jitter: bool = True,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.base_s = base_s
        self.multiplier = multiplier
        self.max_s = max_s
        self.jitter = jitter

    def compute(self) -> float:
        delay = self.base_s * (self.multiplier**self.state.attempts)
        delay = min(delay, self.max_s)
        if self.jitter:
            delay *= random.uniform(0.75, 1.25)
        return delay


class DecorrelatedJitterBackoff(BaseBackoff):
    """AWS-recommended decorrelated jitter (avoids thundering herd)."""

    def __init__(self, base_s: float = 0.5, max_s: float = 30.0, **kwargs):
        super().__init__(**kwargs)
        self.base_s = base_s
        self.max_s = max_s
        self._last = base_s

    def compute(self) -> float:
        self._last = random.uniform(self.base_s, self._last * 3)
        return min(self._last, self.max_s)


class AdaptiveBackoff(BaseBackoff):
    """Adapts based on observed success/failure ratio."""

    def __init__(self, base_s: float = 1.0, max_s: float = 60.0, **kwargs):
        super().__init__(**kwargs)
        self.base_s = base_s
        self.max_s = max_s
        self._successes = 0
        self._failures = 0

    def record_success(self):
        self._successes += 1

    def record_failure(self):
        self._failures += 1

    def compute(self) -> float:
        total = self._successes + self._failures
        if total == 0:
            return self.base_s
        failure_rate = self._failures / total
        # Scale delay by failure rate — more failures → longer wait
        delay = (
            self.base_s * (1 + failure_rate * 4) * (2 ** min(self.state.attempts, 6))
        )
        return min(delay, self.max_s)


class TokenBucketBackoff(BaseBackoff):
    """Rate-limits based on token bucket — waits until a token is available."""

    def __init__(self, rate_per_s: float = 1.0, burst: int = 3, **kwargs):
        super().__init__(**kwargs)
        self.rate_per_s = rate_per_s
        self.burst = burst
        self._tokens = float(burst)
        self._last_refill = time.time()

    def _refill(self):
        now = time.time()
        elapsed = now - self._last_refill
        self._tokens = min(self.burst, self._tokens + elapsed * self.rate_per_s)
        self._last_refill = now

    def compute(self) -> float:
        self._refill()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return 0.0
        deficit = 1.0 - self._tokens
        return deficit / self.rate_per_s

    async def wait(self) -> bool:
        delay = self.compute()
        if delay > 0:
            await asyncio.sleep(delay)
            self._tokens = max(0.0, self._tokens - 1.0)
        self.state.attempts += 1
        self.state.total_wait_s += delay
        return True


class BackoffFactory:
    """Factory for creating backoff instances by name."""

    @staticmethod
    def create(
        strategy: str = "exponential",
        **kwargs,
    ) -> BaseBackoff:
        strategies = {
            "fixed": FixedBackoff,
            "linear": LinearBackoff,
            "exponential": ExponentialBackoff,
            "decorrelated": DecorrelatedJitterBackoff,
            "adaptive": AdaptiveBackoff,
            "token_bucket": TokenBucketBackoff,
        }
        cls = strategies.get(strategy, ExponentialBackoff)
        return cls(**kwargs)

    @staticmethod
    def for_llm_rate_limit() -> ExponentialBackoff:
        """Pre-configured for LLM API rate limit (429) handling."""
        return ExponentialBackoff(base_s=2.0, multiplier=2.0, max_s=120.0, jitter=True)

    @staticmethod
    def for_network_retry() -> DecorrelatedJitterBackoff:
        """Pre-configured for network transient failures."""
        return DecorrelatedJitterBackoff(base_s=0.5, max_s=30.0)

    @staticmethod
    def for_cluster_probe() -> TokenBucketBackoff:
        """Pre-configured for cluster health probing."""
        return TokenBucketBackoff(rate_per_s=0.5, burst=2)


async def main():
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        strategies = [
            ("fixed", FixedBackoff(delay_s=0.1)),
            ("linear", LinearBackoff(base_s=0.1, increment_s=0.1, max_s=1.0)),
            ("exponential", ExponentialBackoff(base_s=0.1, jitter=False)),
            ("decorrelated", DecorrelatedJitterBackoff(base_s=0.05, max_s=1.0)),
        ]

        print(f"{'Strategy':<15} {'Delays (s)'}")
        print("-" * 60)
        for name, backoff in strategies:
            delays = []
            for _ in range(5):
                delays.append(round(backoff.compute(), 3))
                backoff.state.attempts += 1
            print(f"  {name:<15} {delays}")

    elif cmd == "rate-limit":
        print("Token bucket test (5 req/s burst=3):")
        tb = TokenBucketBackoff(rate_per_s=1.0, burst=3)
        for i in range(6):
            t0 = time.time()
            await tb.wait()
            elapsed = time.time() - t0
            print(f"  Request {i + 1}: waited {elapsed:.2f}s")


if __name__ == "__main__":
    asyncio.run(main())
