#!/usr/bin/env python3
"""
jarvis_load_balancer_v2 — Advanced LLM request load balancer
Weighted round-robin, least-connections, latency-aware routing with health checks
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from statistics import mean
from typing import Any

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.load_balancer_v2")

REDIS_KEY = "jarvis:lb:stats"


class Strategy(str, Enum):
    ROUND_ROBIN = "round_robin"
    WEIGHTED = "weighted"
    LEAST_CONN = "least_conn"
    LATENCY = "latency"
    RANDOM = "random"


@dataclass
class Backend:
    name: str
    url: str
    weight: float = 1.0
    max_connections: int = 10
    active_connections: int = 0
    total_requests: int = 0
    total_errors: int = 0
    latency_history: list[float] = field(default_factory=list)
    healthy: bool = True
    last_check: float = 0.0
    models: list[str] = field(default_factory=list)

    @property
    def error_rate(self) -> float:
        if not self.total_requests:
            return 0.0
        return self.total_errors / self.total_requests

    @property
    def avg_latency_ms(self) -> float:
        if not self.latency_history:
            return 9999.0
        return mean(self.latency_history[-20:])

    @property
    def score(self) -> float:
        """Lower = better for routing decisions."""
        if not self.healthy:
            return float("inf")
        base = self.avg_latency_ms / max(self.weight, 0.1)
        conn_penalty = self.active_connections * 50
        err_penalty = self.error_rate * 1000
        return base + conn_penalty + err_penalty

    def record_latency(self, ms: float):
        self.latency_history.append(ms)
        if len(self.latency_history) > 100:
            self.latency_history = self.latency_history[-100:]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "url": self.url,
            "weight": self.weight,
            "healthy": self.healthy,
            "active_connections": self.active_connections,
            "total_requests": self.total_requests,
            "error_rate": round(self.error_rate, 4),
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "score": round(self.score, 1) if self.score != float("inf") else None,
            "models": self.models[:5],
        }


class LoadBalancerV2:
    def __init__(self, strategy: Strategy = Strategy.LATENCY):
        self.strategy = strategy
        self.redis: aioredis.Redis | None = None
        self._backends: dict[str, Backend] = {}
        self._rr_index: int = 0
        self._health_task: asyncio.Task | None = None
        self._register_defaults()

    def _register_defaults(self):
        self.add_backend(Backend(name="M1", url="http://127.0.0.1:1234", weight=2.0))
        self.add_backend(Backend(name="M2", url="http://192.168.1.26:1234", weight=1.5))

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def add_backend(self, backend: Backend):
        self._backends[backend.name] = backend
        log.info(f"Added backend: {backend.name} ({backend.url})")

    def remove_backend(self, name: str):
        self._backends.pop(name, None)

    def _healthy_backends(self) -> list[Backend]:
        return [b for b in self._backends.values() if b.healthy]

    def _select(self, model: str | None = None) -> Backend | None:
        candidates = self._healthy_backends()
        if not candidates:
            return None

        # Filter by model availability if specified
        if model:
            model_aware = [b for b in candidates if any(model in m for m in b.models)]
            if model_aware:
                candidates = model_aware

        if self.strategy == Strategy.ROUND_ROBIN:
            idx = self._rr_index % len(candidates)
            self._rr_index += 1
            return candidates[idx]

        elif self.strategy == Strategy.WEIGHTED:
            import random

            total_w = sum(b.weight for b in candidates)
            r = random.uniform(0, total_w)
            cumulative = 0.0
            for b in candidates:
                cumulative += b.weight
                if r <= cumulative:
                    return b
            return candidates[-1]

        elif self.strategy == Strategy.LEAST_CONN:
            return min(candidates, key=lambda b: b.active_connections)

        elif self.strategy == Strategy.LATENCY:
            return min(candidates, key=lambda b: b.score)

        elif self.strategy == Strategy.RANDOM:
            import random

            return random.choice(candidates)

        return candidates[0]

    async def route(
        self,
        path: str,
        method: str = "POST",
        body: dict | None = None,
        model: str | None = None,
        timeout_s: float = 60.0,
    ) -> tuple[Any, str]:
        """Route request to best backend. Returns (response_data, backend_name)."""
        backend = self._select(model)
        if not backend:
            raise RuntimeError("No healthy backends available")

        backend.active_connections += 1
        backend.total_requests += 1
        t0 = time.time()

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout_s)
            ) as sess:
                fn = getattr(sess, method.lower())
                async with fn(f"{backend.url}{path}", json=body) as r:
                    latency_ms = (time.time() - t0) * 1000
                    backend.record_latency(latency_ms)
                    if r.status >= 500:
                        backend.total_errors += 1
                    data = await r.json()
                    data["_backend"] = backend.name
                    data["_latency_ms"] = round(latency_ms, 1)
                    return data, backend.name

        except Exception as e:
            backend.total_errors += 1
            latency_ms = (time.time() - t0) * 1000
            backend.record_latency(latency_ms)
            log.error(f"Backend {backend.name} error: {e}")

            # Try failover
            fallback = self._select_excluding(backend.name, model)
            if fallback:
                log.info(f"Failover: {backend.name} → {fallback.name}")
                return await self.route(path, method, body, model, timeout_s)
            raise

        finally:
            backend.active_connections = max(0, backend.active_connections - 1)
            if self.redis:
                await self._sync_stats()

    def _select_excluding(
        self, exclude: str, model: str | None = None
    ) -> Backend | None:
        candidates = [b for b in self._healthy_backends() if b.name != exclude]
        if not candidates:
            return None
        return min(candidates, key=lambda b: b.score)

    async def health_check(self) -> dict[str, bool]:
        results = {}
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=3)
        ) as sess:
            for name, backend in self._backends.items():
                t0 = time.time()
                try:
                    async with sess.get(f"{backend.url}/v1/models") as r:
                        latency_ms = (time.time() - t0) * 1000
                        healthy = r.status == 200
                        if healthy:
                            data = await r.json()
                            backend.models = [
                                m["id"] for m in data.get("data", [])[:10]
                            ]
                        backend.healthy = healthy
                        backend.record_latency(latency_ms)
                        results[name] = healthy
                except Exception:
                    backend.healthy = False
                    results[name] = False
                backend.last_check = time.time()
        return results

    async def start_health_loop(self, interval_s: float = 30.0):
        async def loop():
            while True:
                try:
                    results = await self.health_check()
                    healthy = sum(1 for v in results.values() if v)
                    log.debug(f"Health check: {healthy}/{len(results)} backends up")
                except Exception as e:
                    log.error(f"Health loop error: {e}")
                await asyncio.sleep(interval_s)

        self._health_task = asyncio.create_task(loop())

    async def _sync_stats(self):
        if not self.redis:
            return
        stats = {name: b.to_dict() for name, b in self._backends.items()}
        await self.redis.set(REDIS_KEY, json.dumps(stats), ex=300)

    def stats(self) -> list[dict]:
        return sorted(
            [b.to_dict() for b in self._backends.values()],
            key=lambda x: x.get("score") or 9999,
        )


async def main():
    import sys

    lb = LoadBalancerV2(Strategy.LATENCY)
    await lb.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"

    if cmd == "check":
        results = await lb.health_check()
        for name, healthy in results.items():
            b = lb._backends[name]
            status = "✅" if healthy else "❌"
            models = ", ".join(b.models[:3]) if b.models else "—"
            print(f"  {status} {name:<6} {b.avg_latency_ms:.0f}ms  {models}")

    elif cmd == "stats":
        await lb.health_check()
        print(
            f"{'Backend':<8} {'Score':>8} {'Latency':>10} {'Errors':>8} {'Connections':>12}"
        )
        print("-" * 55)
        for s in lb.stats():
            score = f"{s['score']:.1f}" if s["score"] is not None else "∞"
            print(
                f"  {s['name']:<8} {score:>8} {s['avg_latency_ms']:>8.0f}ms {s['error_rate']:>7.2%} {s['active_connections']:>11}"
            )

    elif cmd == "route" and len(sys.argv) > 2:
        await lb.health_check()
        path = sys.argv[2]
        body = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
        data, backend = await lb.route(path, body=body)
        print(f"Routed to: {backend}")
        print(json.dumps(data, indent=2)[:500])


if __name__ == "__main__":
    asyncio.run(main())
