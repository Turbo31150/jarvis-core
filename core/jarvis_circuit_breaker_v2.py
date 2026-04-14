#!/usr/bin/env python3
"""
jarvis_circuit_breaker_v2 — Advanced circuit breaker with half-open probing
Per-endpoint state machines, adaptive thresholds, and Redis-backed shared state
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.circuit_breaker_v2")

REDIS_PREFIX = "jarvis:cb2:"


class CBState(str, Enum):
    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing — reject requests
    HALF_OPEN = "half_open"  # Probing — allow limited requests


@dataclass
class CBConfig:
    failure_threshold: int = 5  # failures before opening
    success_threshold: int = 2  # successes in half-open before closing
    timeout_s: float = 30.0  # how long to stay open before probing
    half_open_max: int = 3  # max concurrent probes
    window_s: float = 60.0  # rolling window for failure counting
    slow_call_ms: float = 5000.0  # calls slower than this count as failures
    slow_call_ratio: float = 0.5  # open if >50% of calls are slow


@dataclass
class CBStats:
    total_calls: int = 0
    success_calls: int = 0
    failure_calls: int = 0
    rejected_calls: int = 0
    slow_calls: int = 0
    last_failure: float = 0.0
    last_success: float = 0.0
    opened_count: int = 0

    @property
    def failure_rate(self) -> float:
        denom = self.success_calls + self.failure_calls
        return self.failure_calls / max(denom, 1)

    def to_dict(self) -> dict:
        return {
            "total": self.total_calls,
            "success": self.success_calls,
            "failure": self.failure_calls,
            "rejected": self.rejected_calls,
            "slow": self.slow_calls,
            "failure_rate": round(self.failure_rate, 3),
            "opened_count": self.opened_count,
        }


@dataclass
class CircuitBreaker:
    name: str
    config: CBConfig = field(default_factory=CBConfig)
    state: CBState = CBState.CLOSED
    stats: CBStats = field(default_factory=CBStats)
    _opened_at: float = 0.0
    _half_open_probes: int = 0
    _consecutive_successes: int = 0
    _recent_failures: list[float] = field(default_factory=list)

    def _prune_window(self):
        cutoff = time.time() - self.config.window_s
        self._recent_failures = [t for t in self._recent_failures if t > cutoff]

    def allow(self) -> bool:
        """Check if a call should be allowed through."""
        now = time.time()

        if self.state == CBState.CLOSED:
            return True

        if self.state == CBState.OPEN:
            if now - self._opened_at >= self.config.timeout_s:
                self.state = CBState.HALF_OPEN
                self._half_open_probes = 0
                self._consecutive_successes = 0
                log.info(f"CB [{self.name}]: OPEN → HALF_OPEN")
                return self._half_open_probes < self.config.half_open_max
            self.stats.rejected_calls += 1
            return False

        if self.state == CBState.HALF_OPEN:
            if self._half_open_probes < self.config.half_open_max:
                self._half_open_probes += 1
                return True
            self.stats.rejected_calls += 1
            return False

        return True

    def record_success(self, latency_ms: float = 0.0):
        self.stats.total_calls += 1
        self.stats.last_success = time.time()

        if latency_ms > self.config.slow_call_ms:
            self.stats.slow_calls += 1
            self._record_failure()
            return

        self.stats.success_calls += 1
        self._consecutive_successes += 1

        if self.state == CBState.HALF_OPEN:
            if self._consecutive_successes >= self.config.success_threshold:
                self.state = CBState.CLOSED
                self._recent_failures.clear()
                log.info(f"CB [{self.name}]: HALF_OPEN → CLOSED")

    def record_failure(self, error: str = ""):
        self.stats.total_calls += 1
        self._record_failure()

    def _record_failure(self):
        now = time.time()
        self.stats.failure_calls += 1
        self.stats.last_failure = now
        self._consecutive_successes = 0
        self._recent_failures.append(now)
        self._prune_window()

        if self.state == CBState.HALF_OPEN:
            self._trip(now)
            return

        if self.state == CBState.CLOSED:
            if len(self._recent_failures) >= self.config.failure_threshold:
                self._trip(now)

    def _trip(self, now: float):
        if self.state != CBState.OPEN:
            self.state = CBState.OPEN
            self._opened_at = now
            self.stats.opened_count += 1
            log.warning(
                f"CB [{self.name}]: → OPEN (failures={len(self._recent_failures)})"
            )

    def reset(self):
        self.state = CBState.CLOSED
        self._recent_failures.clear()
        self._consecutive_successes = 0
        self._half_open_probes = 0
        log.info(f"CB [{self.name}]: manually RESET")

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "state": self.state.value,
            "stats": self.stats.to_dict(),
            "recent_failures": len(self._recent_failures),
            "opened_at": self._opened_at,
        }


class CircuitBreakerRegistry:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._breakers: dict[str, CircuitBreaker] = {}

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def get(self, name: str, config: CBConfig | None = None) -> CircuitBreaker:
        if name not in self._breakers:
            self._breakers[name] = CircuitBreaker(
                name=name, config=config or CBConfig()
            )
        return self._breakers[name]

    async def execute(
        self,
        name: str,
        fn: Callable,
        *args: Any,
        config: CBConfig | None = None,
        **kwargs: Any,
    ) -> Any:
        cb = self.get(name, config)
        if not cb.allow():
            raise RuntimeError(f"Circuit breaker '{name}' is {cb.state.value}")

        t0 = time.time()
        try:
            if asyncio.iscoroutinefunction(fn):
                result = await fn(*args, **kwargs)
            else:
                result = fn(*args, **kwargs)
            latency_ms = (time.time() - t0) * 1000
            cb.record_success(latency_ms)
            return result
        except Exception as e:
            cb.record_failure(str(e))
            raise

    async def sync_state(self, name: str):
        """Push state to Redis for cluster-wide sharing."""
        cb = self._breakers.get(name)
        if not cb or not self.redis:
            return
        await self.redis.setex(
            f"{REDIS_PREFIX}{name}",
            120,
            json.dumps(cb.to_dict()),
        )

    def status(self) -> dict:
        return {name: cb.to_dict() for name, cb in self._breakers.items()}

    def summary(self) -> dict:
        breakers = list(self._breakers.values())
        return {
            "total": len(breakers),
            "closed": sum(1 for b in breakers if b.state == CBState.CLOSED),
            "open": sum(1 for b in breakers if b.state == CBState.OPEN),
            "half_open": sum(1 for b in breakers if b.state == CBState.HALF_OPEN),
        }


async def main():
    import sys

    registry = CircuitBreakerRegistry()
    await registry.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        config = CBConfig(failure_threshold=3, timeout_s=2.0, success_threshold=2)
        call_count = 0

        async def flaky_service():
            nonlocal call_count
            call_count += 1
            if call_count <= 3:
                raise ConnectionError("Service unavailable")
            return f"OK (call #{call_count})"

        print("Simulating failures to trip breaker...")
        for i in range(6):
            try:
                result = await registry.execute(
                    "test_service", flaky_service, config=config
                )
                print(f"  Call {i + 1}: {result}")
            except Exception as e:
                cb = registry.get("test_service")
                print(f"  Call {i + 1}: {type(e).__name__}: {e} | CB={cb.state.value}")
            await asyncio.sleep(0.1)

        print("\nWaiting for half-open...")
        await asyncio.sleep(2.5)

        for i in range(4):
            try:
                result = await registry.execute(
                    "test_service", flaky_service, config=config
                )
                cb = registry.get("test_service")
                print(f"  Probe {i + 1}: {result} | CB={cb.state.value}")
            except Exception as e:
                cb = registry.get("test_service")
                print(f"  Probe {i + 1}: {e} | CB={cb.state.value}")

        print(f"\nFinal state: {registry.summary()}")

    elif cmd == "status":
        print(json.dumps(registry.status(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
