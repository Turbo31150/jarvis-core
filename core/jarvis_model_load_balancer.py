#!/usr/bin/env python3
"""
jarvis_model_load_balancer — Dynamic load balancing across LLM model instances
Tracks backend health, active requests, latency EMA, and routes optimally
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.model_load_balancer")

REDIS_PREFIX = "jarvis:mlb:"


class BalancePolicy(str, Enum):
    ROUND_ROBIN = "round_robin"
    LEAST_CONN = "least_conn"
    WEIGHTED_LATENCY = "weighted_latency"
    RANDOM = "random"
    STICKY = "sticky"  # same model for same session_id


class BackendHealth(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass
class ModelBackend:
    name: str
    url: str
    model: str
    max_concurrency: int = 4
    weight: float = 1.0
    tags: list[str] = field(default_factory=list)

    # Runtime state
    active: int = 0
    total_requests: int = 0
    total_errors: int = 0
    latency_ema: float = 0.0  # exponential moving average ms
    last_check: float = 0.0
    health: BackendHealth = BackendHealth.HEALTHY
    consecutive_errors: int = 0

    @property
    def available(self) -> bool:
        return (
            self.health != BackendHealth.UNHEALTHY
            and self.active < self.max_concurrency
        )

    @property
    def load_factor(self) -> float:
        return self.active / max(self.max_concurrency, 1)

    @property
    def error_rate(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return self.total_errors / self.total_requests

    def update_latency(self, latency_ms: float, alpha: float = 0.2):
        if self.latency_ema == 0.0:
            self.latency_ema = latency_ms
        else:
            self.latency_ema = alpha * latency_ms + (1 - alpha) * self.latency_ema

    def update_health(self):
        if self.consecutive_errors >= 5:
            self.health = BackendHealth.UNHEALTHY
        elif self.consecutive_errors >= 2 or self.error_rate > 0.3:
            self.health = BackendHealth.DEGRADED
        else:
            self.health = BackendHealth.HEALTHY

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "model": self.model,
            "health": self.health.value,
            "active": self.active,
            "load_factor": round(self.load_factor, 2),
            "latency_ema_ms": round(self.latency_ema, 1),
            "error_rate": round(self.error_rate, 3),
            "total_requests": self.total_requests,
        }


class ModelLoadBalancer:
    def __init__(self, policy: BalancePolicy = BalancePolicy.WEIGHTED_LATENCY):
        self.redis: aioredis.Redis | None = None
        self._backends: list[ModelBackend] = []
        self._policy = policy
        self._rr_idx: int = 0
        self._sticky_map: dict[str, str] = {}  # session_id → backend name
        self._stats: dict[str, int] = {
            "routed": 0,
            "errors": 0,
            "no_backend": 0,
            "health_checks": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def add_backend(self, backend: ModelBackend):
        self._backends.append(backend)
        log.info(f"Backend added: {backend.name} ({backend.model})")

    def remove_backend(self, name: str):
        self._backends = [b for b in self._backends if b.name != name]

    def _candidates(self, model_filter: str | None = None) -> list[ModelBackend]:
        backends = [b for b in self._backends if b.available]
        if model_filter:
            filtered = [b for b in backends if b.model == model_filter]
            if filtered:
                return filtered
        return backends

    def select(
        self,
        model: str | None = None,
        session_id: str | None = None,
        tags: list[str] | None = None,
    ) -> ModelBackend | None:
        candidates = self._candidates(model)

        if tags:
            tagged = [b for b in candidates if any(t in b.tags for t in tags)]
            if tagged:
                candidates = tagged

        if not candidates:
            self._stats["no_backend"] += 1
            return None

        if self._policy == BalancePolicy.STICKY and session_id:
            name = self._sticky_map.get(session_id)
            if name:
                pinned = next((b for b in candidates if b.name == name), None)
                if pinned:
                    return pinned
            # Assign
            chosen = self._select_least_conn(candidates)
            if chosen:
                self._sticky_map[session_id] = chosen.name
            return chosen

        if self._policy == BalancePolicy.ROUND_ROBIN:
            backend = candidates[self._rr_idx % len(candidates)]
            self._rr_idx += 1
            return backend

        if self._policy == BalancePolicy.LEAST_CONN:
            return self._select_least_conn(candidates)

        if self._policy == BalancePolicy.WEIGHTED_LATENCY:
            return self._select_weighted_latency(candidates)

        if self._policy == BalancePolicy.RANDOM:
            import random

            return random.choice(candidates)

        return candidates[0]

    def _select_least_conn(self, candidates: list[ModelBackend]) -> ModelBackend:
        return min(candidates, key=lambda b: b.active)

    def _select_weighted_latency(self, candidates: list[ModelBackend]) -> ModelBackend:
        # Score = load_factor * 0.5 + normalized_latency * 0.5, weighted by backend.weight
        latencies = [b.latency_ema for b in candidates if b.latency_ema > 0]
        max_lat = max(latencies) if latencies else 1.0

        def score(b: ModelBackend) -> float:
            lat_norm = (b.latency_ema / max(max_lat, 1.0)) if b.latency_ema > 0 else 0.5
            raw = b.load_factor * 0.6 + lat_norm * 0.4
            return raw / max(b.weight, 0.01)  # lower is better

        return min(candidates, key=score)

    async def call(
        self,
        messages: list[dict],
        model: str | None = None,
        session_id: str | None = None,
        params: dict | None = None,
    ) -> dict[str, Any]:
        backend = self.select(model=model, session_id=session_id)
        if not backend:
            self._stats["errors"] += 1
            raise RuntimeError("No available backends")

        self._stats["routed"] += 1
        backend.active += 1
        backend.total_requests += 1
        t0 = time.time()

        try:
            payload = {
                "model": model or backend.model,
                "messages": messages,
                **(params or {}),
            }
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60)
            ) as sess:
                async with sess.post(
                    f"{backend.url}/v1/chat/completions", json=payload
                ) as r:
                    lat = (time.time() - t0) * 1000
                    backend.update_latency(lat)
                    if r.status == 200:
                        data = await r.json(content_type=None)
                        backend.consecutive_errors = 0
                        backend.update_health()
                        return {
                            "content": data["choices"][0]["message"]["content"],
                            "backend": backend.name,
                            "latency_ms": lat,
                        }
                    else:
                        raise RuntimeError(f"HTTP {r.status}")
        except Exception:
            backend.total_errors += 1
            backend.consecutive_errors += 1
            backend.update_health()
            self._stats["errors"] += 1
            raise
        finally:
            backend.active -= 1

    async def health_check_all(self):
        for backend in self._backends:
            self._stats["health_checks"] += 1
            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=3)
                ) as sess:
                    async with sess.get(f"{backend.url}/v1/models") as r:
                        if r.status == 200:
                            backend.consecutive_errors = 0
                        else:
                            backend.consecutive_errors += 1
            except Exception:
                backend.consecutive_errors += 1
            backend.update_health()
            backend.last_check = time.time()

    def list_backends(self) -> list[dict]:
        return [b.to_dict() for b in self._backends]

    def stats(self) -> dict:
        healthy = sum(1 for b in self._backends if b.health == BackendHealth.HEALTHY)
        return {
            **self._stats,
            "backends": len(self._backends),
            "healthy": healthy,
            "policy": self._policy.value,
        }


def build_jarvis_load_balancer() -> ModelLoadBalancer:
    lb = ModelLoadBalancer(policy=BalancePolicy.WEIGHTED_LATENCY)
    lb.add_backend(
        ModelBackend(
            "m1-qwen9b",
            "http://192.168.1.85:1234",
            "qwen3.5-9b",
            max_concurrency=4,
            weight=1.2,
            tags=["fast"],
        )
    )
    lb.add_backend(
        ModelBackend(
            "m1-qwen27b",
            "http://192.168.1.85:1234",
            "qwen3.5-27b",
            max_concurrency=2,
            weight=1.0,
            tags=["quality"],
        )
    )
    lb.add_backend(
        ModelBackend(
            "m2-qwen9b",
            "http://192.168.1.26:1234",
            "qwen3.5-9b",
            max_concurrency=3,
            weight=0.9,
            tags=["fast"],
        )
    )
    lb.add_backend(
        ModelBackend(
            "ol1-gemma",
            "http://127.0.0.1:11434",
            "gemma3:4b",
            max_concurrency=1,
            weight=0.5,
            tags=["local"],
        )
    )
    return lb


async def main():
    import sys

    lb = build_jarvis_load_balancer()
    await lb.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        await lb.health_check_all()
        print("Backends:")
        for b in lb.list_backends():
            print(f"  {b['name']:<20} health={b['health']:<10} load={b['load_factor']}")

        # Simulate selections
        for i in range(10):
            b = lb.select()
            if b:
                print(f"  Selected[{i}]: {b.name}")
        print(f"\nStats: {json.dumps(lb.stats(), indent=2)}")

    elif cmd == "health":
        await lb.health_check_all()
        for b in lb.list_backends():
            status = "✅" if b["health"] == "healthy" else "❌"
            print(f"  {status} {b['name']:<20} {b['health']}")

    elif cmd == "stats":
        print(json.dumps(lb.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
