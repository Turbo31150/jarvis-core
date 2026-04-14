#!/usr/bin/env python3
"""
jarvis_retry_manager — Configurable retry engine with multiple backoff strategies
Handles transient failures for LLM calls, HTTP requests, and arbitrary async operations
"""

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.retry_manager")

REDIS_PREFIX = "jarvis:retry:"


class BackoffStrategy(str, Enum):
    FIXED = "fixed"
    LINEAR = "linear"
    EXPONENTIAL = "exponential"
    EXPONENTIAL_JITTER = "exponential_jitter"
    FIBONACCI = "fibonacci"


@dataclass
class RetryConfig:
    max_attempts: int = 3
    base_delay_s: float = 1.0
    max_delay_s: float = 60.0
    backoff: BackoffStrategy = BackoffStrategy.EXPONENTIAL_JITTER
    jitter_factor: float = 0.25
    retryable_exceptions: tuple = (Exception,)
    non_retryable_exceptions: tuple = ()
    retry_on_status: set[int] = field(default_factory=lambda: {429, 500, 502, 503, 504})
    timeout_s: float = 0.0  # 0 = no per-attempt timeout


@dataclass
class RetryAttempt:
    attempt: int
    error: str
    delay_s: float
    ts: float = field(default_factory=time.time)


@dataclass
class RetryResult:
    success: bool
    value: Any
    attempts: int
    total_time_ms: float
    history: list[RetryAttempt] = field(default_factory=list)
    final_error: str = ""

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "attempts": self.attempts,
            "total_time_ms": round(self.total_time_ms, 1),
            "final_error": self.final_error,
            "history": [
                {"attempt": a.attempt, "error": a.error[:100], "delay_s": a.delay_s}
                for a in self.history
            ],
        }


def _fibonacci_sequence(n: int) -> list[float]:
    seq = [1.0, 1.0]
    while len(seq) < n:
        seq.append(seq[-1] + seq[-2])
    return seq


class RetryManager:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._stats: dict[str, int] = {
            "total": 0,
            "succeeded": 0,
            "failed": 0,
            "retried": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def _compute_delay(self, attempt: int, config: RetryConfig) -> float:
        base = config.base_delay_s
        strategy = config.backoff

        if strategy == BackoffStrategy.FIXED:
            delay = base
        elif strategy == BackoffStrategy.LINEAR:
            delay = base * attempt
        elif strategy == BackoffStrategy.EXPONENTIAL:
            delay = base * (2 ** (attempt - 1))
        elif strategy == BackoffStrategy.EXPONENTIAL_JITTER:
            delay = base * (2 ** (attempt - 1))
            jitter = delay * config.jitter_factor
            delay = delay + random.uniform(-jitter, jitter)
        elif strategy == BackoffStrategy.FIBONACCI:
            fib = _fibonacci_sequence(attempt + 1)
            delay = base * fib[attempt - 1]
        else:
            delay = base

        return min(max(delay, 0.0), config.max_delay_s)

    def _is_retryable(self, exc: Exception, config: RetryConfig) -> bool:
        if isinstance(exc, config.non_retryable_exceptions):
            return False
        # Check HTTP status codes embedded in exception message
        msg = str(exc)
        for status in config.retry_on_status:
            if str(status) in msg:
                return True
        return isinstance(exc, config.retryable_exceptions)

    async def execute(
        self,
        fn: Callable,
        *args: Any,
        config: RetryConfig | None = None,
        operation_name: str = "unknown",
        **kwargs: Any,
    ) -> RetryResult:
        cfg = config or RetryConfig()
        t0 = time.time()
        history: list[RetryAttempt] = []
        self._stats["total"] += 1

        for attempt in range(1, cfg.max_attempts + 1):
            try:
                if cfg.timeout_s > 0:
                    coro = (
                        fn(*args, **kwargs) if asyncio.iscoroutinefunction(fn) else None
                    )
                    if coro:
                        value = await asyncio.wait_for(coro, timeout=cfg.timeout_s)
                    else:
                        value = fn(*args, **kwargs)
                else:
                    if asyncio.iscoroutinefunction(fn):
                        value = await fn(*args, **kwargs)
                    else:
                        value = fn(*args, **kwargs)

                elapsed = (time.time() - t0) * 1000
                self._stats["succeeded"] += 1
                log.debug(
                    f"[{operation_name}] succeeded on attempt {attempt}/{cfg.max_attempts} "
                    f"in {elapsed:.0f}ms"
                )
                return RetryResult(
                    success=True,
                    value=value,
                    attempts=attempt,
                    total_time_ms=elapsed,
                    history=history,
                )

            except Exception as exc:
                error_str = f"{type(exc).__name__}: {exc}"

                if attempt == cfg.max_attempts or not self._is_retryable(exc, cfg):
                    elapsed = (time.time() - t0) * 1000
                    self._stats["failed"] += 1
                    log.error(
                        f"[{operation_name}] failed after {attempt} attempts: {error_str}"
                    )
                    return RetryResult(
                        success=False,
                        value=None,
                        attempts=attempt,
                        total_time_ms=elapsed,
                        history=history,
                        final_error=error_str,
                    )

                delay = self._compute_delay(attempt, cfg)
                history.append(
                    RetryAttempt(attempt=attempt, error=error_str, delay_s=delay)
                )
                self._stats["retried"] += 1
                log.warning(
                    f"[{operation_name}] attempt {attempt} failed: {error_str}. "
                    f"Retrying in {delay:.1f}s..."
                )
                await asyncio.sleep(delay)

        # Should not reach here
        return RetryResult(
            success=False,
            value=None,
            attempts=cfg.max_attempts,
            total_time_ms=(time.time() - t0) * 1000,
        )

    async def execute_or_raise(
        self,
        fn: Callable,
        *args: Any,
        config: RetryConfig | None = None,
        operation_name: str = "unknown",
        **kwargs: Any,
    ) -> Any:
        """Like execute() but raises on failure instead of returning RetryResult."""
        result = await self.execute(
            fn, *args, config=config, operation_name=operation_name, **kwargs
        )
        if not result.success:
            raise RuntimeError(f"[{operation_name}] failed: {result.final_error}")
        return result.value

    def stats(self) -> dict:
        total = max(self._stats["total"], 1)
        return {
            **self._stats,
            "success_rate": round(self._stats["succeeded"] / total, 3),
        }


# Convenience decorators


def with_retry(
    max_attempts: int = 3,
    base_delay_s: float = 1.0,
    backoff: BackoffStrategy = BackoffStrategy.EXPONENTIAL_JITTER,
):
    """Decorator that wraps an async function with retry logic."""
    config = RetryConfig(
        max_attempts=max_attempts,
        base_delay_s=base_delay_s,
        backoff=backoff,
    )
    manager = RetryManager()

    def decorator(fn: Callable) -> Callable:
        async def wrapper(*args, **kwargs):
            result = await manager.execute(
                fn, *args, config=config, operation_name=fn.__name__, **kwargs
            )
            if not result.success:
                raise RuntimeError(
                    f"{fn.__name__} failed after {result.attempts} attempts: {result.final_error}"
                )
            return result.value

        wrapper.__name__ = fn.__name__
        return wrapper

    return decorator


async def main():
    import sys

    mgr = RetryManager()
    await mgr.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        call_count = 0

        async def flaky(succeed_on: int = 3):
            nonlocal call_count
            call_count += 1
            if call_count < succeed_on:
                raise ConnectionError(f"Connection refused (attempt {call_count})")
            return f"Success on attempt {call_count}"

        configs = [
            ("fixed", RetryConfig(backoff=BackoffStrategy.FIXED, base_delay_s=0.1)),
            (
                "exp_jitter",
                RetryConfig(
                    backoff=BackoffStrategy.EXPONENTIAL_JITTER, base_delay_s=0.1
                ),
            ),
            (
                "fibonacci",
                RetryConfig(backoff=BackoffStrategy.FIBONACCI, base_delay_s=0.05),
            ),
        ]

        for name, cfg in configs:
            call_count = 0
            t0 = time.time()
            result = await mgr.execute(flaky, 3, config=cfg, operation_name=name)
            elapsed = (time.time() - t0) * 1000
            status = "✅" if result.success else "❌"
            print(
                f"{status} [{name}] {result.attempts} attempts, {elapsed:.0f}ms: {result.value or result.final_error}"
            )
            for h in result.history:
                print(
                    f"    attempt {h.attempt}: {h.error[:50]} → wait {h.delay_s:.2f}s"
                )

        print(f"\nStats: {json.dumps(mgr.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(mgr.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
