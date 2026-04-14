#!/usr/bin/env python3
"""
jarvis_model_timeout — Adaptive timeout management for LLM inference requests
Tracks latency histograms per model, adapts timeouts, detects slow outliers
"""

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.model_timeout")

REDIS_PREFIX = "jarvis:timeout:"


class TimeoutPolicy(str, Enum):
    FIXED = "fixed"  # constant timeout regardless of history
    ADAPTIVE = "adaptive"  # p95 * multiplier
    PERCENTILE = "percentile"  # configurable percentile * multiplier
    DEADLINE = "deadline"  # hard absolute deadline


@dataclass
class TimeoutConfig:
    policy: TimeoutPolicy = TimeoutPolicy.ADAPTIVE
    base_timeout_s: float = 30.0  # fallback when no history
    min_timeout_s: float = 5.0
    max_timeout_s: float = 120.0
    percentile: float = 95.0  # p95 by default
    multiplier: float = 1.5  # safety margin over percentile
    sample_window: int = 100  # latency samples to keep


@dataclass
class LatencySample:
    model: str
    latency_s: float
    success: bool
    ts: float = field(default_factory=time.time)


@dataclass
class ModelTimeoutState:
    model: str
    config: TimeoutConfig
    samples: list[LatencySample] = field(default_factory=list)
    timeouts_triggered: int = 0
    total_requests: int = 0

    @property
    def p_latency(self) -> float:
        """Compute configured percentile latency from samples."""
        successful = [s.latency_s for s in self.samples if s.success]
        if not successful:
            return self.config.base_timeout_s
        sorted_lats = sorted(successful)
        idx = min(
            int(math.ceil(len(sorted_lats) * self.config.percentile / 100)) - 1,
            len(sorted_lats) - 1,
        )
        return sorted_lats[max(0, idx)]

    @property
    def current_timeout_s(self) -> float:
        cfg = self.config
        if cfg.policy == TimeoutPolicy.FIXED:
            return cfg.base_timeout_s
        if cfg.policy in (TimeoutPolicy.ADAPTIVE, TimeoutPolicy.PERCENTILE):
            if not self.samples:
                return cfg.base_timeout_s
            pct = self.p_latency
            timeout = pct * cfg.multiplier
            return max(cfg.min_timeout_s, min(cfg.max_timeout_s, timeout))
        return cfg.base_timeout_s

    @property
    def timeout_rate(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return self.timeouts_triggered / self.total_requests

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "current_timeout_s": round(self.current_timeout_s, 2),
            "p_latency_s": round(self.p_latency, 3),
            "sample_count": len(self.samples),
            "timeout_rate": round(self.timeout_rate, 4),
            "timeouts_triggered": self.timeouts_triggered,
            "policy": self.config.policy.value,
        }


class ModelTimeoutManager:
    def __init__(self, default_config: TimeoutConfig | None = None):
        self.redis: aioredis.Redis | None = None
        self._states: dict[str, ModelTimeoutState] = {}
        self._default_config = default_config or TimeoutConfig()
        self._stats: dict[str, int] = {
            "requests": 0,
            "timeouts": 0,
            "successes": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def configure(self, model: str, config: TimeoutConfig):
        if model not in self._states:
            self._states[model] = ModelTimeoutState(model=model, config=config)
        else:
            self._states[model].config = config

    def _get_or_create(self, model: str) -> ModelTimeoutState:
        if model not in self._states:
            self._states[model] = ModelTimeoutState(
                model=model, config=self._default_config
            )
        return self._states[model]

    def get_timeout(self, model: str) -> float:
        return self._get_or_create(model).current_timeout_s

    def record(self, model: str, latency_s: float, success: bool):
        state = self._get_or_create(model)
        sample = LatencySample(model=model, latency_s=latency_s, success=success)
        state.samples.append(sample)
        if len(state.samples) > state.config.sample_window:
            state.samples.pop(0)
        state.total_requests += 1
        if success:
            self._stats["successes"] += 1
        else:
            state.timeouts_triggered += 1
            self._stats["timeouts"] += 1

        if self.redis:
            asyncio.create_task(
                self.redis.setex(
                    f"{REDIS_PREFIX}{model}",
                    300,
                    json.dumps(state.to_dict()),
                )
            )

    async def run_with_timeout(
        self,
        model: str,
        coro,
        on_timeout: Callable | None = None,
    ) -> tuple[Any, float]:
        """Run coroutine with adaptive timeout. Returns (result, latency_s)."""
        timeout_s = self.get_timeout(model)
        t0 = time.time()
        self._stats["requests"] += 1
        try:
            result = await asyncio.wait_for(coro, timeout=timeout_s)
            latency = time.time() - t0
            self.record(model, latency, success=True)
            return result, latency
        except asyncio.TimeoutError:
            latency = time.time() - t0
            self.record(model, latency, success=False)
            log.warning(
                f"Timeout for {model} after {latency:.1f}s (limit={timeout_s:.1f}s)"
            )
            if on_timeout:
                on_timeout(model, latency, timeout_s)
            raise

    def suggest_timeout(self, model: str) -> dict:
        """Diagnostic: what timeout should be used and why."""
        state = self._get_or_create(model)
        return {
            "model": model,
            "suggested_timeout_s": round(state.current_timeout_s, 2),
            "p_latency_s": round(state.p_latency, 3),
            "multiplier": state.config.multiplier,
            "samples": len(state.samples),
            "policy": state.config.policy.value,
            "reasoning": (
                f"p{state.config.percentile:.0f}={state.p_latency:.2f}s × {state.config.multiplier} "
                f"= {state.p_latency * state.config.multiplier:.2f}s "
                f"(clamped to [{state.config.min_timeout_s}, {state.config.max_timeout_s}])"
            ),
        }

    def all_states(self) -> list[dict]:
        return [s.to_dict() for s in self._states.values()]

    def stats(self) -> dict:
        return {**self._stats, "models_tracked": len(self._states)}


def build_jarvis_timeout_manager() -> ModelTimeoutManager:
    mgr = ModelTimeoutManager(
        default_config=TimeoutConfig(
            policy=TimeoutPolicy.ADAPTIVE,
            base_timeout_s=30.0,
            min_timeout_s=5.0,
            max_timeout_s=120.0,
            multiplier=1.5,
        )
    )
    # Model-specific configs
    mgr.configure(
        "gemma3:4b",
        TimeoutConfig(
            policy=TimeoutPolicy.ADAPTIVE, base_timeout_s=10.0, max_timeout_s=30.0
        ),
    )
    mgr.configure(
        "qwen3.5-9b", TimeoutConfig(policy=TimeoutPolicy.ADAPTIVE, base_timeout_s=20.0)
    )
    mgr.configure(
        "deepseek-r1-0528",
        TimeoutConfig(
            policy=TimeoutPolicy.ADAPTIVE, base_timeout_s=60.0, max_timeout_s=180.0
        ),
    )
    return mgr


async def main():
    import sys
    import random

    mgr = build_jarvis_timeout_manager()
    await mgr.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        random.seed(42)
        models = ["qwen3.5-9b", "deepseek-r1-0528", "gemma3:4b"]

        print("Simulating 50 requests per model...")
        for model in models:
            for _ in range(50):
                # Simulate latency distribution
                base = {"qwen3.5-9b": 0.8, "deepseek-r1-0528": 3.5, "gemma3:4b": 0.3}[
                    model
                ]
                latency = abs(random.gauss(base, base * 0.3))
                success = random.random() > 0.05  # 5% timeout rate
                mgr.record(model, latency, success)

        print("\nTimeout suggestions:")
        for model in models:
            s = mgr.suggest_timeout(model)
            print(
                f"  {model:<25} timeout={s['suggested_timeout_s']:.2f}s  {s['reasoning']}"
            )

        print("\nAll states:")
        for s in mgr.all_states():
            print(
                f"  {s['model']:<25} timeout={s['current_timeout_s']:.2f}s "
                f"p_lat={s['p_latency_s']:.3f}s  timeout_rate={s['timeout_rate']:.2%}"
            )

        print(f"\nStats: {json.dumps(mgr.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(mgr.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
