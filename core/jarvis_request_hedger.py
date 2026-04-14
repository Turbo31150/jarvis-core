#!/usr/bin/env python3
"""
jarvis_request_hedger — Hedged requests to reduce tail latency
Sends duplicate requests after a delay and returns the first successful response
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.request_hedger")

REDIS_PREFIX = "jarvis:hedger:"


@dataclass
class HedgeConfig:
    hedge_delay_ms: float = 200.0  # send hedge after this delay
    max_hedges: int = 2  # max parallel copies (1 original + N hedges)
    timeout_s: float = 30.0
    cancel_on_first: bool = True  # cancel remaining when first succeeds
    min_latency_threshold_ms: float = 100.0  # only hedge if expected latency > this


@dataclass
class HedgeAttempt:
    attempt_id: int
    backend: str
    started_at: float
    completed_at: float = 0.0
    success: bool = False
    content: str = ""
    error: str = ""

    @property
    def latency_ms(self) -> float:
        if self.completed_at > 0:
            return (self.completed_at - self.started_at) * 1000
        return 0.0

    def to_dict(self) -> dict:
        return {
            "attempt_id": self.attempt_id,
            "backend": self.backend,
            "latency_ms": round(self.latency_ms, 1),
            "success": self.success,
            "won": False,
        }


@dataclass
class HedgeResult:
    content: str
    winner_attempt: int
    winner_backend: str
    winner_latency_ms: float
    total_attempts: int
    hedge_savings_ms: float  # latency saved vs slowest successful attempt
    all_attempts: list[HedgeAttempt]
    duration_ms: float

    def to_dict(self) -> dict:
        attempts = [a.to_dict() for a in self.all_attempts]
        for a in attempts:
            a["won"] = a["attempt_id"] == self.winner_attempt
        return {
            "winner_attempt": self.winner_attempt,
            "winner_backend": self.winner_backend,
            "winner_latency_ms": round(self.winner_latency_ms, 1),
            "hedge_savings_ms": round(self.hedge_savings_ms, 1),
            "total_attempts": self.total_attempts,
            "duration_ms": round(self.duration_ms, 1),
            "attempts": attempts,
        }


class RequestHedger:
    def __init__(self, config: HedgeConfig | None = None):
        self.redis: aioredis.Redis | None = None
        self._config = config or HedgeConfig()
        self._backends: list[dict] = []
        self._latency_ema: dict[str, float] = {}  # backend → EMA latency ms
        self._stats: dict[str, int] = {
            "requests": 0,
            "hedges_sent": 0,
            "hedge_wins": 0,  # times hedge was faster than original
            "timeouts": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def add_backend(self, name: str, url: str, model: str):
        self._backends.append({"name": name, "url": url, "model": model})

    def _update_ema(self, backend: str, latency_ms: float, alpha: float = 0.2):
        prev = self._latency_ema.get(backend, 0.0)
        self._latency_ema[backend] = (
            alpha * latency_ms + (1 - alpha) * prev if prev > 0 else latency_ms
        )

    def _should_hedge(self, backend: str) -> bool:
        ema = self._latency_ema.get(backend, 0.0)
        return ema >= self._config.min_latency_threshold_ms

    async def _single_request(
        self,
        attempt_id: int,
        backend: dict,
        messages: list[dict],
        params: dict,
    ) -> HedgeAttempt:
        attempt = HedgeAttempt(
            attempt_id=attempt_id,
            backend=backend["name"],
            started_at=time.time(),
        )
        try:
            payload = {
                "model": backend["model"],
                "messages": messages,
                **params,
            }
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._config.timeout_s)
            ) as sess:
                async with sess.post(
                    f"{backend['url']}/v1/chat/completions", json=payload
                ) as r:
                    attempt.completed_at = time.time()
                    if r.status == 200:
                        data = await r.json(content_type=None)
                        attempt.content = data["choices"][0]["message"]["content"]
                        attempt.success = True
                        self._update_ema(backend["name"], attempt.latency_ms)
                    else:
                        attempt.error = f"HTTP {r.status}"
        except asyncio.CancelledError:
            attempt.completed_at = time.time()
            attempt.error = "cancelled"
        except Exception as e:
            attempt.completed_at = time.time()
            attempt.error = str(e)[:80]
        return attempt

    async def hedge(
        self,
        messages: list[dict],
        params: dict | None = None,
        backend_names: list[str] | None = None,
    ) -> HedgeResult:
        t0 = time.time()
        self._stats["requests"] += 1
        params = params or {}
        cfg = self._config

        # Select backends
        available = [
            b
            for b in self._backends
            if backend_names is None or b["name"] in backend_names
        ]
        if not available:
            raise RuntimeError("No backends configured")

        # Pick original + hedge backends
        targets = available[: cfg.max_hedges]
        all_attempts: list[HedgeAttempt] = []

        if len(targets) == 1 or not self._should_hedge(targets[0]["name"]):
            # No hedge — single request
            attempt = await self._single_request(0, targets[0], messages, params)
            all_attempts.append(attempt)
            if not attempt.success:
                raise RuntimeError(attempt.error)
            return HedgeResult(
                content=attempt.content,
                winner_attempt=0,
                winner_backend=attempt.backend,
                winner_latency_ms=attempt.latency_ms,
                total_attempts=1,
                hedge_savings_ms=0.0,
                all_attempts=all_attempts,
                duration_ms=(time.time() - t0) * 1000,
            )

        # Launch original request
        result_queue: asyncio.Queue = asyncio.Queue()
        tasks: list[asyncio.Task] = []

        async def run_attempt(idx: int, backend: dict, delay: float = 0.0):
            if delay > 0:
                await asyncio.sleep(delay / 1000.0)
                self._stats["hedges_sent"] += 1
            attempt = await self._single_request(idx, backend, messages, params)
            all_attempts.append(attempt)
            if attempt.success:
                await result_queue.put(attempt)

        for i, backend in enumerate(targets):
            delay = cfg.hedge_delay_ms * i
            task = asyncio.create_task(run_attempt(i, backend, delay))
            tasks.append(task)

        # Wait for first success or all failures
        winner: HedgeAttempt | None = None
        try:
            winner = await asyncio.wait_for(result_queue.get(), timeout=cfg.timeout_s)
        except asyncio.TimeoutError:
            self._stats["timeouts"] += 1

        # Cancel remaining if configured
        if cfg.cancel_on_first:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        if winner is None:
            # All failed — collect errors
            if not tasks:
                raise RuntimeError("No attempts completed")
            raise RuntimeError(f"All {len(tasks)} hedged requests failed")

        if winner.attempt_id > 0:
            self._stats["hedge_wins"] += 1

        # Compute savings: difference between winner and slowest other successful
        other_latencies = [
            a.latency_ms
            for a in all_attempts
            if a.success and a.attempt_id != winner.attempt_id
        ]
        savings = max(other_latencies) - winner.latency_ms if other_latencies else 0.0

        return HedgeResult(
            content=winner.content,
            winner_attempt=winner.attempt_id,
            winner_backend=winner.backend,
            winner_latency_ms=winner.latency_ms,
            total_attempts=len(all_attempts),
            hedge_savings_ms=max(0.0, savings),
            all_attempts=all_attempts,
            duration_ms=(time.time() - t0) * 1000,
        )

    def backend_latencies(self) -> dict[str, float]:
        return {k: round(v, 1) for k, v in self._latency_ema.items()}

    def stats(self) -> dict:
        return {
            **self._stats,
            "backends": len(self._backends),
            "latencies": self.backend_latencies(),
        }


def build_jarvis_hedger() -> RequestHedger:
    hedger = RequestHedger(
        HedgeConfig(hedge_delay_ms=150, max_hedges=2, min_latency_threshold_ms=50)
    )
    hedger.add_backend("m1", "http://192.168.1.85:1234", "qwen3.5-9b")
    hedger.add_backend("m2", "http://192.168.1.26:1234", "qwen3.5-9b")
    return hedger


async def main():
    import sys

    hedger = build_jarvis_hedger()
    await hedger.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        messages = [{"role": "user", "content": "Say 'pong' in one word."}]
        try:
            result = await hedger.hedge(messages)
            print(
                f"Winner: attempt={result.winner_attempt} backend={result.winner_backend}"
            )
            print(
                f"Latency: {result.winner_latency_ms:.0f}ms  savings: {result.hedge_savings_ms:.0f}ms"
            )
            print(f"Attempts: {result.total_attempts}")
        except Exception as e:
            print(f"Failed: {e}")
        print(f"\nStats: {json.dumps(hedger.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(hedger.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
