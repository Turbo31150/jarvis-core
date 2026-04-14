#!/usr/bin/env python3
"""
jarvis_rate_window — Sliding window and fixed window rate tracking primitives
Low-level building blocks for rate limiting, burst detection, and quota enforcement
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from threading import Lock
from typing import Any


log = logging.getLogger("jarvis.rate_window")

REDIS_PREFIX = "jarvis:ratewin:"


class WindowType(str, Enum):
    FIXED = "fixed"
    SLIDING = "sliding"
    TOKEN_BUCKET = "token_bucket"
    LEAKY_BUCKET = "leaky_bucket"


from enum import Enum


class WindowType(str, Enum):
    FIXED = "fixed"
    SLIDING = "sliding"
    TOKEN_BUCKET = "token_bucket"
    LEAKY_BUCKET = "leaky_bucket"


@dataclass
class WindowConfig:
    window_s: float = 1.0
    limit: int = 100
    burst: int = 0  # token bucket burst (0 = use limit)
    window_type: WindowType = WindowType.SLIDING


@dataclass
class WindowState:
    count: int
    limit: int
    remaining: int
    reset_in_s: float
    window_type: WindowType
    allowed: bool

    def to_dict(self) -> dict:
        return {
            "count": self.count,
            "limit": self.limit,
            "remaining": self.remaining,
            "reset_in_s": round(self.reset_in_s, 2),
            "window_type": self.window_type.value,
            "allowed": self.allowed,
        }


class SlidingWindowCounter:
    """Thread-safe sliding window counter."""

    def __init__(self, window_s: float = 1.0, limit: int = 100):
        self._window = window_s
        self._limit = limit
        self._timestamps: list[float] = []
        self._lock = Lock()

    def _prune(self) -> None:
        cutoff = time.time() - self._window
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.pop(0)

    def count(self) -> int:
        with self._lock:
            self._prune()
            return len(self._timestamps)

    def check(self, n: int = 1) -> WindowState:
        with self._lock:
            self._prune()
            current = len(self._timestamps)
            allowed = (current + n) <= self._limit
            remaining = max(0, self._limit - current)
            reset_in = (
                self._window - (time.time() - self._timestamps[0])
                if self._timestamps
                else 0.0
            )
            return WindowState(
                current,
                self._limit,
                remaining,
                max(0, reset_in),
                WindowType.SLIDING,
                allowed,
            )

    def add(self, n: int = 1) -> WindowState:
        state = self.check(n)
        if state.allowed:
            with self._lock:
                now = time.time()
                self._timestamps.extend([now] * n)
        return state

    def reset(self):
        with self._lock:
            self._timestamps.clear()


class FixedWindowCounter:
    """Fixed window (per second/minute/etc.) counter."""

    def __init__(self, window_s: float = 1.0, limit: int = 100):
        self._window = window_s
        self._limit = limit
        self._count = 0
        self._window_start = time.time()
        self._lock = Lock()

    def _maybe_reset(self) -> None:
        now = time.time()
        if (now - self._window_start) >= self._window:
            self._count = 0
            self._window_start = now

    def check(self, n: int = 1) -> WindowState:
        with self._lock:
            self._maybe_reset()
            allowed = (self._count + n) <= self._limit
            remaining = max(0, self._limit - self._count)
            reset_in = self._window - (time.time() - self._window_start)
            return WindowState(
                self._count,
                self._limit,
                remaining,
                max(0, reset_in),
                WindowType.FIXED,
                allowed,
            )

    def add(self, n: int = 1) -> WindowState:
        with self._lock:
            self._maybe_reset()
            allowed = (self._count + n) <= self._limit
            if allowed:
                self._count += n
            remaining = max(0, self._limit - self._count)
            reset_in = self._window - (time.time() - self._window_start)
            return WindowState(
                self._count,
                self._limit,
                remaining,
                max(0, reset_in),
                WindowType.FIXED,
                allowed,
            )

    def reset(self):
        with self._lock:
            self._count = 0
            self._window_start = time.time()


class TokenBucket:
    """Token bucket with configurable refill rate and burst."""

    def __init__(self, rate: float = 10.0, burst: int = 0):
        self._rate = rate  # tokens per second
        self._burst = burst or int(rate)
        self._tokens = float(self._burst)
        self._last_refill = time.time()
        self._lock = Lock()

    def _refill(self) -> None:
        now = time.time()
        elapsed = now - self._last_refill
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
        self._last_refill = now

    def check(self, n: float = 1.0) -> WindowState:
        with self._lock:
            self._refill()
            allowed = self._tokens >= n
            return WindowState(
                count=int(self._burst - self._tokens),
                limit=self._burst,
                remaining=max(0, int(self._tokens)),
                reset_in_s=(n - self._tokens) / max(self._rate, 1e-9)
                if not allowed
                else 0.0,
                window_type=WindowType.TOKEN_BUCKET,
                allowed=allowed,
            )

    def consume(self, n: float = 1.0) -> WindowState:
        with self._lock:
            self._refill()
            allowed = self._tokens >= n
            if allowed:
                self._tokens -= n
            return WindowState(
                count=int(self._burst - self._tokens),
                limit=self._burst,
                remaining=max(0, int(self._tokens)),
                reset_in_s=(n - self._tokens) / max(self._rate, 1e-9)
                if not allowed
                else 0.0,
                window_type=WindowType.TOKEN_BUCKET,
                allowed=allowed,
            )

    def fill(self):
        with self._lock:
            self._tokens = float(self._burst)


class MultiKeyRateWindow:
    """Rate windows per entity (user, model, IP, etc.)."""

    def __init__(self, config: WindowConfig):
        self._config = config
        self._windows: dict[str, Any] = {}
        self._stats: dict[str, int] = {"allowed": 0, "denied": 0}

    def _get_window(self, key: str) -> Any:
        if key not in self._windows:
            cfg = self._config
            if cfg.window_type == WindowType.SLIDING:
                self._windows[key] = SlidingWindowCounter(cfg.window_s, cfg.limit)
            elif cfg.window_type == WindowType.FIXED:
                self._windows[key] = FixedWindowCounter(cfg.window_s, cfg.limit)
            elif cfg.window_type == WindowType.TOKEN_BUCKET:
                self._windows[key] = TokenBucket(
                    cfg.limit / cfg.window_s, cfg.burst or cfg.limit
                )
            else:
                self._windows[key] = SlidingWindowCounter(cfg.window_s, cfg.limit)
        return self._windows[key]

    def check(self, key: str, n: int = 1) -> WindowState:
        w = self._get_window(key)
        if hasattr(w, "consume"):
            return w.check(n)
        return w.check(n)

    def consume(self, key: str, n: int = 1) -> WindowState:
        w = self._get_window(key)
        if hasattr(w, "consume"):
            state = w.consume(n)
        else:
            state = w.add(n)
        if state.allowed:
            self._stats["allowed"] += 1
        else:
            self._stats["denied"] += 1
        return state

    def reset(self, key: str):
        if key in self._windows:
            self._windows[key].reset()

    def active_keys(self) -> list[str]:
        return list(self._windows.keys())

    def stats(self) -> dict:
        return {
            **self._stats,
            "active_keys": len(self._windows),
            "config": self._config.window_type.value,
        }


async def main():
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("=== Sliding Window ===")
        sw = SlidingWindowCounter(window_s=1.0, limit=5)
        for i in range(8):
            state = sw.add()
            print(
                f"  [{i + 1}] allowed={state.allowed} count={state.count} remaining={state.remaining}"
            )

        print("\n=== Token Bucket ===")
        tb = TokenBucket(rate=10.0, burst=5)
        for i in range(8):
            state = tb.consume(1)
            print(f"  [{i + 1}] allowed={state.allowed} tokens_left={state.remaining}")

        print("\n=== Multi-Key Rate Window ===")
        mw = MultiKeyRateWindow(
            WindowConfig(window_s=1.0, limit=3, window_type=WindowType.SLIDING)
        )
        keys = ["user_a", "user_b", "user_a", "user_a", "user_b", "user_a"]
        for k in keys:
            state = mw.consume(k)
            print(f"  {k:<10} allowed={state.allowed} remaining={state.remaining}")
        print(f"\nStats: {json.dumps(mw.stats(), indent=2)}")

    elif cmd == "stats":
        mw = MultiKeyRateWindow(WindowConfig())
        print(json.dumps(mw.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
