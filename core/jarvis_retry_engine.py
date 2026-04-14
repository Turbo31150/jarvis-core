#!/usr/bin/env python3
"""
jarvis_retry_engine — Smart retry with exponential backoff + circuit breaker
Wraps LLM calls with retry logic, failover, jitter, and state tracking
"""

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass
from enum import Enum

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.retry_engine")

REDIS_KEY = "jarvis:retry_engine"


class CircuitState(Enum):
    CLOSED = "closed"  # normal operation
    OPEN = "open"  # failing, reject requests
    HALF_OPEN = "half_open"  # testing recovery


@dataclass
class RetryConfig:
    max_attempts: int = 3
    base_delay_s: float = 1.0
    max_delay_s: float = 30.0
    backoff_factor: float = 2.0
    jitter: bool = True
    retryable_exceptions: tuple = (Exception,)
    retryable_status_codes: tuple = (429, 500, 502, 503, 504)


@dataclass
class CircuitBreaker:
    name: str
    failure_threshold: int = 5
    recovery_timeout_s: float = 60.0
    half_open_max_calls: int = 2
    # State
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    last_failure_time: float = 0.0
    half_open_calls: int = 0
    total_trips: int = 0

    def record_success(self):
        if self.state == CircuitState.HALF_OPEN:
            self.success_count += 1
            if self.success_count >= self.half_open_max_calls:
                self.state = CircuitState.CLOSED
                self.failure_count = 0
                self.success_count = 0
                self.half_open_calls = 0
                log.info(f"Circuit [{self.name}] CLOSED (recovered)")
        elif self.state == CircuitState.CLOSED:
            self.failure_count = max(0, self.failure_count - 1)

    def record_failure(self):
        self.failure_count += 1
        self.last_failure_time = time.time()

        if self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.OPEN
            self.half_open_calls = 0
            log.warning(f"Circuit [{self.name}] back to OPEN (half-open failed)")

        elif (
            self.state == CircuitState.CLOSED
            and self.failure_count >= self.failure_threshold
        ):
            self.state = CircuitState.OPEN
            self.total_trips += 1
            log.error(f"Circuit [{self.name}] OPENED (trip #{self.total_trips})")

    def can_execute(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            if time.time() - self.last_failure_time >= self.recovery_timeout_s:
                self.state = CircuitState.HALF_OPEN
                self.half_open_calls = 0
                self.success_count = 0
                log.info(f"Circuit [{self.name}] HALF-OPEN (testing)")
                return True
            return False
        if self.state == CircuitState.HALF_OPEN:
            if self.half_open_calls < self.half_open_max_calls:
                self.half_open_calls += 1
                return True
            return False
        return False

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "state": self.state.value,
            "failures": self.failure_count,
            "trips": self.total_trips,
            "last_failure_ago_s": round(time.time() - self.last_failure_time, 1)
            if self.last_failure_time
            else None,
        }


class RetryEngine:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._circuits: dict[str, CircuitBreaker] = {}
        self._stats = {
            "total_calls": 0,
            "retried": 0,
            "circuit_blocked": 0,
            "failed": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def get_circuit(self, name: str) -> CircuitBreaker:
        if name not in self._circuits:
            self._circuits[name] = CircuitBreaker(name=name)
        return self._circuits[name]

    def _compute_delay(self, attempt: int, config: RetryConfig) -> float:
        delay = min(
            config.base_delay_s * (config.backoff_factor**attempt), config.max_delay_s
        )
        if config.jitter:
            delay *= 0.5 + random.random() * 0.5
        return delay

    async def execute(
        self,
        coro_factory,
        circuit_name: str = "default",
        config: RetryConfig | None = None,
        label: str = "",
    ):
        """Execute coro_factory() with retry + circuit breaker."""
        config = config or RetryConfig()
        circuit = self.get_circuit(circuit_name)
        self._stats["total_calls"] += 1

        last_error: Exception | None = None

        for attempt in range(config.max_attempts):
            if not circuit.can_execute():
                self._stats["circuit_blocked"] += 1
                raise RuntimeError(f"Circuit [{circuit_name}] is {circuit.state.value}")

            try:
                result = await coro_factory()
                circuit.record_success()
                if attempt > 0:
                    log.info(
                        f"[{label or circuit_name}] succeeded on attempt {attempt + 1}"
                    )
                return result

            except Exception as e:
                last_error = e
                circuit.record_failure()

                if attempt + 1 >= config.max_attempts:
                    break

                # Check if retryable
                is_retryable = isinstance(e, config.retryable_exceptions)
                if not is_retryable:
                    break

                delay = self._compute_delay(attempt, config)
                self._stats["retried"] += 1
                log.warning(
                    f"[{label or circuit_name}] attempt {attempt + 1} failed: {e}. "
                    f"Retrying in {delay:.1f}s"
                )
                await asyncio.sleep(delay)

        self._stats["failed"] += 1
        raise last_error or RuntimeError("All attempts failed")

    async def execute_with_fallback(
        self,
        primary_factory,
        fallback_factory,
        circuit_name: str = "default",
        config: RetryConfig | None = None,
    ):
        """Try primary with retry, fall back to secondary on final failure."""
        try:
            return await self.execute(
                primary_factory, circuit_name, config, label="primary"
            )
        except Exception as e:
            log.warning(f"Primary failed ({e}), trying fallback")
            return await fallback_factory()

    def status(self) -> dict:
        return {
            "stats": self._stats,
            "circuits": {name: cb.to_dict() for name, cb in self._circuits.items()},
        }

    async def save_status(self):
        if self.redis:
            await self.redis.set(REDIS_KEY, json.dumps(self.status()), ex=300)


async def main():
    import sys

    engine = RetryEngine()
    await engine.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        call_count = 0

        async def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError(f"Simulated failure #{call_count}")
            return f"Success on call #{call_count}"

        try:
            result = await engine.execute(
                flaky, circuit_name="demo", label="flaky_service"
            )
            print(f"Result: {result}")
        except Exception as e:
            print(f"Final error: {e}")

        print(json.dumps(engine.status(), indent=2))

    elif cmd == "status":
        print(json.dumps(engine.status(), indent=2))

    elif cmd == "reset" and len(sys.argv) > 2:
        name = sys.argv[2]
        if name in engine._circuits:
            cb = engine._circuits[name]
            cb.state = CircuitState.CLOSED
            cb.failure_count = 0
            print(f"Reset circuit: {name}")
        else:
            print(f"Circuit not found: {name}")


if __name__ == "__main__":
    asyncio.run(main())
