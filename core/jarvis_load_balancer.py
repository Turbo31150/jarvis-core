#!/usr/bin/env python3
"""
jarvis_load_balancer — Adaptive load balancer for LLM inference backends
Weighted round-robin, least-connections, latency-aware, and consistent-hash strategies
"""

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.load_balancer")

REDIS_PREFIX = "jarvis:lb:"


class LBStrategy(str, Enum):
    ROUND_ROBIN = "round_robin"
    WEIGHTED_RR = "weighted_rr"
    LEAST_CONN = "least_conn"
    LATENCY_AWARE = "latency_aware"
    CONSISTENT_HASH = "consistent_hash"
    RANDOM = "random"


class BackendStatus(str, Enum):
    UP = "up"
    DOWN = "down"
    DRAINING = "draining"  # no new requests, drain existing
    DEGRADED = "degraded"  # up but slow


@dataclass
class Backend:
    backend_id: str
    url: str
    weight: int = 1
    max_connections: int = 0  # 0 = unlimited
    status: BackendStatus = BackendStatus.UP
    tags: list[str] = field(default_factory=list)
    # Runtime metrics
    active_connections: int = 0
    total_requests: int = 0
    total_errors: int = 0
    _latency_samples: list[float] = field(default_factory=list, repr=False)
    last_check: float = 0.0

    @property
    def avg_latency_ms(self) -> float:
        if not self._latency_samples:
            return 0.0
        return sum(self._latency_samples[-20:]) / min(len(self._latency_samples), 20)

    @property
    def error_rate(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return self.total_errors / self.total_requests

    @property
    def is_available(self) -> bool:
        if self.status not in (BackendStatus.UP, BackendStatus.DEGRADED):
            return False
        if self.max_connections > 0 and self.active_connections >= self.max_connections:
            return False
        return True

    def record_latency(self, ms: float):
        self._latency_samples.append(ms)
        if len(self._latency_samples) > 100:
            self._latency_samples.pop(0)

    def score(self) -> float:
        """Lower is better for selection."""
        if not self.is_available:
            return float("inf")
        latency_factor = max(self.avg_latency_ms, 1.0)
        conn_factor = max(self.active_connections, 0) + 1
        error_penalty = 1.0 + self.error_rate * 5
        return (latency_factor * conn_factor * error_penalty) / max(self.weight, 1)

    def to_dict(self) -> dict:
        return {
            "backend_id": self.backend_id,
            "url": self.url,
            "weight": self.weight,
            "status": self.status.value,
            "active_connections": self.active_connections,
            "total_requests": self.total_requests,
            "error_rate": round(self.error_rate, 4),
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "score": round(self.score(), 2),
            "tags": self.tags,
        }


class LoadBalancer:
    def __init__(self, strategy: LBStrategy = LBStrategy.LATENCY_AWARE):
        self.redis: aioredis.Redis | None = None
        self._backends: dict[str, Backend] = {}
        self._strategy = strategy
        self._rr_index: int = 0
        self._wrr_state: dict[str, int] = {}  # current weight counters
        self._stats: dict[str, int] = {
            "requests": 0,
            "no_backend": 0,
            "errors": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def add_backend(self, backend: Backend):
        self._backends[backend.backend_id] = backend
        self._wrr_state[backend.backend_id] = 0

    def remove_backend(self, backend_id: str):
        self._backends.pop(backend_id, None)
        self._wrr_state.pop(backend_id, None)

    def set_status(self, backend_id: str, status: BackendStatus):
        b = self._backends.get(backend_id)
        if b:
            b.status = status
            log.info(f"Backend [{backend_id}] status → {status.value}")

    def _available(self) -> list[Backend]:
        return [b for b in self._backends.values() if b.is_available]

    def _pick_round_robin(self) -> Backend | None:
        avail = self._available()
        if not avail:
            return None
        backend = avail[self._rr_index % len(avail)]
        self._rr_index = (self._rr_index + 1) % max(len(avail), 1)
        return backend

    def _pick_weighted_rr(self) -> Backend | None:
        avail = self._available()
        if not avail:
            return None
        # Increment all weights, pick the one with highest current weight
        for b in avail:
            self._wrr_state[b.backend_id] = (
                self._wrr_state.get(b.backend_id, 0) + b.weight
            )
        best = max(avail, key=lambda b: self._wrr_state.get(b.backend_id, 0))
        self._wrr_state[best.backend_id] -= sum(b.weight for b in avail)
        return best

    def _pick_least_conn(self) -> Backend | None:
        avail = self._available()
        if not avail:
            return None
        return min(avail, key=lambda b: b.active_connections)

    def _pick_latency_aware(self) -> Backend | None:
        avail = self._available()
        if not avail:
            return None
        return min(avail, key=lambda b: b.score())

    def _pick_consistent_hash(self, key: str) -> Backend | None:
        avail = self._available()
        if not avail:
            return None
        h = int(hashlib.md5(key.encode()).hexdigest(), 16)
        # Sort backends deterministically
        sorted_backends = sorted(avail, key=lambda b: b.backend_id)
        return sorted_backends[h % len(sorted_backends)]

    def _pick_random(self) -> Backend | None:
        import random

        avail = self._available()
        return random.choice(avail) if avail else None

    def pick(self, hash_key: str = "") -> Backend | None:
        self._stats["requests"] += 1
        if self._strategy == LBStrategy.ROUND_ROBIN:
            backend = self._pick_round_robin()
        elif self._strategy == LBStrategy.WEIGHTED_RR:
            backend = self._pick_weighted_rr()
        elif self._strategy == LBStrategy.LEAST_CONN:
            backend = self._pick_least_conn()
        elif self._strategy == LBStrategy.LATENCY_AWARE:
            backend = self._pick_latency_aware()
        elif self._strategy == LBStrategy.CONSISTENT_HASH:
            backend = self._pick_consistent_hash(hash_key or str(time.time()))
        else:
            backend = self._pick_random()

        if not backend:
            self._stats["no_backend"] += 1
            log.warning("No available backend")
        return backend

    def acquire(self, backend: Backend):
        backend.active_connections += 1
        backend.total_requests += 1

    def release(self, backend: Backend, latency_ms: float = 0.0, error: bool = False):
        backend.active_connections = max(0, backend.active_connections - 1)
        if latency_ms > 0:
            backend.record_latency(latency_ms)
        if error:
            backend.total_errors += 1
            self._stats["errors"] += 1
            # Auto-degrade if error rate too high
            if backend.error_rate > 0.5 and backend.total_requests > 10:
                backend.status = BackendStatus.DEGRADED

    def backends(self) -> list[dict]:
        return [b.to_dict() for b in self._backends.values()]

    def stats(self) -> dict:
        avail = self._available()
        return {
            **self._stats,
            "strategy": self._strategy.value,
            "total_backends": len(self._backends),
            "available": len(avail),
            "total_active_conn": sum(
                b.active_connections for b in self._backends.values()
            ),
        }


def build_jarvis_load_balancer(
    strategy: LBStrategy = LBStrategy.LATENCY_AWARE,
) -> LoadBalancer:
    lb = LoadBalancer(strategy=strategy)

    lb.add_backend(
        Backend(
            backend_id="m1",
            url="http://192.168.1.85:1234",
            weight=3,
            tags=["fast", "nvidia"],
        )
    )
    lb.add_backend(
        Backend(
            backend_id="m2",
            url="http://192.168.1.26:1234",
            weight=2,
            tags=["large", "nvidia"],
        )
    )
    lb.add_backend(
        Backend(
            backend_id="ol1",
            url="http://127.0.0.1:11434",
            weight=1,
            tags=["local", "amd"],
        )
    )

    return lb


async def main():
    import sys
    import random

    lb = build_jarvis_load_balancer()
    await lb.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Load balancer demo (latency-aware)...")

        # Simulate 20 requests with random latencies
        for i in range(20):
            backend = lb.pick()
            if backend:
                lb.acquire(backend)
                # Simulate work
                latency = random.uniform(100, 2000)
                error = random.random() < 0.05
                await asyncio.sleep(0.01)
                lb.release(backend, latency_ms=latency, error=error)

        print("\nBackend states:")
        for b in lb.backends():
            print(
                f"  {b['backend_id']:<6} {b['status']:<10} "
                f"req={b['total_requests']:<4} "
                f"err_rate={b['error_rate']:.2f} "
                f"avg_lat={b['avg_latency_ms']:.0f}ms "
                f"score={b['score']:.1f}"
            )

        print(f"\nStats: {json.dumps(lb.stats(), indent=2)}")

    elif cmd == "backends":
        for b in lb.backends():
            print(json.dumps(b))

    elif cmd == "stats":
        print(json.dumps(lb.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
