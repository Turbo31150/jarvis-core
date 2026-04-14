#!/usr/bin/env python3
"""
jarvis_resource_pool — Generic async resource pool with lifecycle management
Thread-safe pool for any reusable resource (connections, workers, buffers)
"""

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, TypeVar

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.resource_pool")

REDIS_PREFIX = "jarvis:respool:"

T = TypeVar("T")


class PoolResourceState(str, Enum):
    IDLE = "idle"
    IN_USE = "in_use"
    BROKEN = "broken"
    CLOSING = "closing"


class PoolPolicy(str, Enum):
    FIFO = "fifo"  # first idle resource served first
    LIFO = "lifo"  # last idle resource served first (warmer connections)
    ROUND_ROBIN = "round_robin"
    LEAST_USED = "least_used"


@dataclass
class PooledResource:
    resource_id: str
    resource: Any
    state: PoolResourceState = PoolResourceState.IDLE
    created_at: float = field(default_factory=time.time)
    last_used_at: float = 0.0
    use_count: int = 0
    error_count: int = 0
    max_uses: int = 0  # 0 = unlimited

    @property
    def age_s(self) -> float:
        return time.time() - self.created_at

    @property
    def idle_s(self) -> float:
        if self.last_used_at:
            return time.time() - self.last_used_at
        return self.age_s

    @property
    def exhausted(self) -> bool:
        return self.max_uses > 0 and self.use_count >= self.max_uses

    def to_dict(self) -> dict:
        return {
            "resource_id": self.resource_id,
            "state": self.state.value,
            "age_s": round(self.age_s, 1),
            "idle_s": round(self.idle_s, 1),
            "use_count": self.use_count,
            "error_count": self.error_count,
        }


@dataclass
class PoolConfig:
    name: str
    min_size: int = 1
    max_size: int = 10
    max_idle_s: float = 300.0  # evict idle resources after this
    max_age_s: float = 3600.0  # evict old resources after this
    acquire_timeout_s: float = 30.0
    policy: PoolPolicy = PoolPolicy.LIFO
    validate_on_acquire: bool = False
    max_uses_per_resource: int = 0  # 0 = unlimited


class ResourcePool:
    def __init__(
        self,
        config: PoolConfig,
        factory: Callable,  # async factory() → resource
        validator: Callable | None = None,  # async validator(resource) → bool
        destructor: Callable | None = None,  # async destructor(resource)
    ):
        self.redis: aioredis.Redis | None = None
        self._config = config
        self._factory = factory
        self._validator = validator
        self._destructor = destructor
        self._resources: dict[str, PooledResource] = {}
        self._idle: list[PooledResource] = []
        self._semaphore = asyncio.Semaphore(config.max_size)
        self._lock = asyncio.Lock()
        self._rr_counter = 0
        self._stats: dict[str, int] = {
            "acquired": 0,
            "released": 0,
            "created": 0,
            "destroyed": 0,
            "validation_failures": 0,
            "timeouts": 0,
            "errors": 0,
        }
        self._wait_times: list[float] = []

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    async def _create(self) -> PooledResource:
        resource = await self._factory()
        pr = PooledResource(
            resource_id=str(uuid.uuid4())[:8],
            resource=resource,
            max_uses=self._config.max_uses_per_resource,
        )
        self._resources[pr.resource_id] = pr
        self._stats["created"] += 1
        log.debug(f"Pool '{self._config.name}': created resource {pr.resource_id}")
        return pr

    async def _destroy(self, pr: PooledResource):
        pr.state = PoolResourceState.CLOSING
        if self._destructor:
            try:
                await self._destructor(pr.resource)
            except Exception as e:
                log.warning(f"Destructor error for {pr.resource_id}: {e}")
        self._resources.pop(pr.resource_id, None)
        self._stats["destroyed"] += 1

    async def _validate(self, pr: PooledResource) -> bool:
        if not self._validator:
            return True
        try:
            return await self._validator(pr.resource)
        except Exception:
            return False

    def _pick_idle(self) -> PooledResource | None:
        if not self._idle:
            return None
        policy = self._config.policy
        if policy == PoolPolicy.LIFO:
            return self._idle.pop()
        elif policy == PoolPolicy.FIFO:
            return self._idle.pop(0)
        elif policy == PoolPolicy.ROUND_ROBIN:
            idx = self._rr_counter % len(self._idle)
            self._rr_counter += 1
            pr = self._idle[idx]
            self._idle.pop(idx)
            return pr
        elif policy == PoolPolicy.LEAST_USED:
            pr = min(self._idle, key=lambda r: r.use_count)
            self._idle.remove(pr)
            return pr
        return self._idle.pop()

    @asynccontextmanager
    async def acquire(self):
        t0 = time.time()
        try:
            await asyncio.wait_for(
                self._semaphore.acquire(), timeout=self._config.acquire_timeout_s
            )
        except asyncio.TimeoutError:
            self._stats["timeouts"] += 1
            raise RuntimeError(
                f"Pool '{self._config.name}': acquire timeout "
                f"after {self._config.acquire_timeout_s}s"
            )

        wait_ms = (time.time() - t0) * 1000
        self._wait_times.append(wait_ms)
        if len(self._wait_times) > 200:
            self._wait_times.pop(0)

        pr = None
        try:
            async with self._lock:
                while self._idle:
                    candidate = self._pick_idle()
                    if candidate is None:
                        break
                    # Check expiry
                    if (
                        candidate.idle_s > self._config.max_idle_s
                        or candidate.age_s > self._config.max_age_s
                        or candidate.exhausted
                    ):
                        await self._destroy(candidate)
                        continue
                    # Validate
                    if self._config.validate_on_acquire:
                        valid = await self._validate(candidate)
                        if not valid:
                            self._stats["validation_failures"] += 1
                            await self._destroy(candidate)
                            continue
                    pr = candidate
                    break

                if pr is None:
                    pr = await self._create()

            pr.state = PoolResourceState.IN_USE
            pr.use_count += 1
            pr.last_used_at = time.time()
            self._stats["acquired"] += 1

            yield pr.resource

        except Exception:
            if pr:
                pr.error_count += 1
                pr.state = PoolResourceState.BROKEN
                async with self._lock:
                    await self._destroy(pr)
                pr = None
            self._stats["errors"] += 1
            raise
        finally:
            if pr and pr.state == PoolResourceState.IN_USE:
                pr.state = PoolResourceState.IDLE
                pr.last_used_at = time.time()
                async with self._lock:
                    if pr.exhausted:
                        await self._destroy(pr)
                    else:
                        self._idle.append(pr)
                self._stats["released"] += 1
            self._semaphore.release()

    async def prefill(self):
        """Pre-create min_size resources."""
        async with self._lock:
            while len(self._resources) < self._config.min_size:
                pr = await self._create()
                self._idle.append(pr)

    async def evict_stale(self) -> int:
        evicted = 0
        async with self._lock:
            stale = [
                pr
                for pr in list(self._idle)
                if (
                    pr.idle_s > self._config.max_idle_s
                    or pr.age_s > self._config.max_age_s
                    or pr.exhausted
                )
            ]
            for pr in stale:
                self._idle.remove(pr)
                await self._destroy(pr)
                evicted += 1
        return evicted

    def pool_info(self) -> dict:
        idle = len(self._idle)
        active = sum(
            1 for r in self._resources.values() if r.state == PoolResourceState.IN_USE
        )
        avg_wait = sum(self._wait_times) / max(len(self._wait_times), 1)
        return {
            "name": self._config.name,
            "total": len(self._resources),
            "idle": idle,
            "active": active,
            "max_size": self._config.max_size,
            "utilization_pct": round(active / max(self._config.max_size, 1) * 100, 1),
            "avg_wait_ms": round(avg_wait, 1),
        }

    def stats(self) -> dict:
        return {**self._stats, **self.pool_info()}


def build_jarvis_resource_pool(
    name: str = "default",
    max_size: int = 8,
    factory: Callable | None = None,
) -> ResourcePool:
    async def default_factory():
        return {"id": str(uuid.uuid4())[:8], "created_at": time.time()}

    return ResourcePool(
        config=PoolConfig(name=name, min_size=2, max_size=max_size),
        factory=factory or default_factory,
    )


async def main():
    import sys

    # Demo with a simple dict resource
    pool = build_jarvis_resource_pool("demo-pool", max_size=5)
    await pool.connect_redis()
    await pool.prefill()

    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print(f"Pool pre-filled: {pool.pool_info()}")

        async def worker(wid: int):
            async with pool.acquire() as res:
                await asyncio.sleep(0.05)
                return f"worker-{wid} used {res['id']}"

        print("Running 12 concurrent workers (pool max=5)...")
        results = await asyncio.gather(*[worker(i) for i in range(12)])
        for r in results:
            print(f"  {r}")

        print(f"\nPool info: {pool.pool_info()}")
        stale = await pool.evict_stale()
        print(f"Evicted stale: {stale}")
        import json

        print(f"\nStats: {json.dumps(pool.stats(), indent=2)}")

    elif cmd == "stats":
        import json

        print(json.dumps(pool.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
