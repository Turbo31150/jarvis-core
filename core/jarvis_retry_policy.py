#!/usr/bin/env python3
"""
jarvis_retry_policy — Configurable retry policies with backoff strategies
Exponential, linear, jitter, deadline-aware retries with per-exception routing
"""

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

log = logging.getLogger("jarvis.retry_policy")


class BackoffStrategy(str, Enum):
    FIXED = "fixed"
    LINEAR = "linear"
    EXPONENTIAL = "exponential"
    EXPONENTIAL_JITTER = "exponential_jitter"
    FIBONACCI = "fibonacci"


class RetryOutcome(str, Enum):
    SUCCESS = "success"
    EXHAUSTED = "exhausted"
    DEADLINE = "deadline"
    NON_RETRYABLE = "non_retryable"


@dataclass
class RetryConfig:
    max_attempts: int = 3
    base_delay_s: float = 1.0
    max_delay_s: float = 60.0
    backoff: BackoffStrategy = BackoffStrategy.EXPONENTIAL_JITTER
    multiplier: float = 2.0
    jitter_factor: float = 0.25
    deadline_s: float = 0.0  # 0 = no deadline
    retryable_exceptions: tuple = (Exception,)
    non_retryable_exceptions: tuple = ()


@dataclass
class RetryAttempt:
    attempt: int
    exception: Exception | None
    delay_s: float
    elapsed_s: float
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "attempt": self.attempt,
            "error": str(self.exception)[:100] if self.exception else None,
            "delay_s": round(self.delay_s, 3),
            "elapsed_s": round(self.elapsed_s, 3),
        }


@dataclass
class RetryResult:
    outcome: RetryOutcome
    attempts: list[RetryAttempt]
    total_elapsed_s: float
    final_value: Any = None
    final_error: Exception | None = None

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome.value,
            "attempt_count": len(self.attempts),
            "total_elapsed_s": round(self.total_elapsed_s, 3),
            "attempts": [a.to_dict() for a in self.attempts],
        }


def _fibonacci_sequence(n: int) -> list[int]:
    seq = [1, 1]
    while len(seq) < n:
        seq.append(seq[-1] + seq[-2])
    return seq


class RetryPolicy:
    def __init__(self, config: RetryConfig | None = None):
        self._config = config or RetryConfig()
        self._fib = _fibonacci_sequence(20)
        self._stats: dict[str, int] = {
            "executions": 0,
            "successes": 0,
            "exhausted": 0,
            "deadlines": 0,
            "non_retryable": 0,
            "total_retries": 0,
        }

    def _delay(self, attempt: int) -> float:
        cfg = self._config
        b = cfg.base_delay_s
        m = cfg.multiplier

        if cfg.backoff == BackoffStrategy.FIXED:
            d = b
        elif cfg.backoff == BackoffStrategy.LINEAR:
            d = b * attempt
        elif cfg.backoff == BackoffStrategy.EXPONENTIAL:
            d = b * (m ** (attempt - 1))
        elif cfg.backoff == BackoffStrategy.EXPONENTIAL_JITTER:
            d = b * (m ** (attempt - 1))
            jitter = d * cfg.jitter_factor
            d = d + random.uniform(-jitter, jitter)
        elif cfg.backoff == BackoffStrategy.FIBONACCI:
            idx = min(attempt - 1, len(self._fib) - 1)
            d = b * self._fib[idx]
        else:
            d = b

        return max(0.0, min(d, cfg.max_delay_s))

    async def execute(self, fn: Callable, *args, **kwargs) -> RetryResult:
        self._stats["executions"] += 1
        cfg = self._config
        attempts: list[RetryAttempt] = []
        t_start = time.time()
        deadline = t_start + cfg.deadline_s if cfg.deadline_s > 0 else float("inf")

        for attempt in range(1, cfg.max_attempts + 1):
            elapsed = time.time() - t_start

            if time.time() > deadline:
                self._stats["deadlines"] += 1
                return RetryResult(
                    outcome=RetryOutcome.DEADLINE,
                    attempts=attempts,
                    total_elapsed_s=elapsed,
                    final_error=TimeoutError("Retry deadline exceeded"),
                )

            try:
                if asyncio.iscoroutinefunction(fn):
                    result = await fn(*args, **kwargs)
                else:
                    result = fn(*args, **kwargs)

                self._stats["successes"] += 1
                rec = RetryAttempt(attempt, None, 0.0, time.time() - t_start)
                attempts.append(rec)
                return RetryResult(
                    outcome=RetryOutcome.SUCCESS,
                    attempts=attempts,
                    total_elapsed_s=time.time() - t_start,
                    final_value=result,
                )

            except Exception as exc:
                elapsed = time.time() - t_start

                # Non-retryable?
                if cfg.non_retryable_exceptions and isinstance(
                    exc, cfg.non_retryable_exceptions
                ):
                    self._stats["non_retryable"] += 1
                    rec = RetryAttempt(attempt, exc, 0.0, elapsed)
                    attempts.append(rec)
                    return RetryResult(
                        outcome=RetryOutcome.NON_RETRYABLE,
                        attempts=attempts,
                        total_elapsed_s=elapsed,
                        final_error=exc,
                    )

                if not isinstance(exc, cfg.retryable_exceptions):
                    self._stats["non_retryable"] += 1
                    rec = RetryAttempt(attempt, exc, 0.0, elapsed)
                    attempts.append(rec)
                    return RetryResult(
                        outcome=RetryOutcome.NON_RETRYABLE,
                        attempts=attempts,
                        total_elapsed_s=elapsed,
                        final_error=exc,
                    )

                delay = self._delay(attempt)
                rec = RetryAttempt(attempt, exc, delay, elapsed)
                attempts.append(rec)

                if attempt < cfg.max_attempts:
                    self._stats["total_retries"] += 1
                    log.debug(
                        f"Retry {attempt}/{cfg.max_attempts} after {delay:.2f}s: {exc}"
                    )
                    # Check deadline before sleeping
                    if time.time() + delay > deadline:
                        self._stats["deadlines"] += 1
                        return RetryResult(
                            outcome=RetryOutcome.DEADLINE,
                            attempts=attempts,
                            total_elapsed_s=time.time() - t_start,
                            final_error=exc,
                        )
                    await asyncio.sleep(delay)

        last_exc = attempts[-1].exception if attempts else None
        self._stats["exhausted"] += 1
        return RetryResult(
            outcome=RetryOutcome.EXHAUSTED,
            attempts=attempts,
            total_elapsed_s=time.time() - t_start,
            final_error=last_exc,
        )

    def with_config(self, **kwargs) -> "RetryPolicy":
        """Return a new policy with overridden fields."""
        import dataclasses

        cfg = dataclasses.replace(self._config, **kwargs)
        return RetryPolicy(cfg)

    def stats(self) -> dict:
        return {**self._stats}


# Preset policies
AGGRESSIVE = RetryPolicy(
    RetryConfig(
        max_attempts=5, base_delay_s=0.5, backoff=BackoffStrategy.EXPONENTIAL_JITTER
    )
)
CONSERVATIVE = RetryPolicy(
    RetryConfig(
        max_attempts=3,
        base_delay_s=2.0,
        backoff=BackoffStrategy.EXPONENTIAL,
        max_delay_s=30.0,
    )
)
FAST = RetryPolicy(
    RetryConfig(max_attempts=3, base_delay_s=0.1, backoff=BackoffStrategy.FIXED)
)
NO_RETRY = RetryPolicy(RetryConfig(max_attempts=1))


async def main():
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        policy = RetryPolicy(
            RetryConfig(
                max_attempts=4,
                base_delay_s=0.1,
                backoff=BackoffStrategy.EXPONENTIAL_JITTER,
            )
        )

        # Simulate a flaky function
        call_count = 0

        async def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError(f"Attempt {call_count} failed")
            return "success!"

        result = await policy.execute(flaky)
        print(f"Outcome: {result.outcome.value}")
        print(f"Attempts: {len(result.attempts)}")
        for a in result.attempts:
            print(
                f"  [{a.attempt}] error={a.to_dict()['error']} delay={a.delay_s:.2f}s"
            )
        print(f"Value: {result.final_value}")

        # Test deadline
        async def slow():
            await asyncio.sleep(5)
            return "done"

        deadline_policy = RetryPolicy(
            RetryConfig(max_attempts=3, base_delay_s=0.1, deadline_s=0.5)
        )
        r2 = await deadline_policy.execute(slow)
        print(f"\nDeadline test: {r2.outcome.value} elapsed={r2.total_elapsed_s:.2f}s")

        print(f"\nStats: {json.dumps(policy.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(AGGRESSIVE.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
