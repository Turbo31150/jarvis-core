#!/usr/bin/env python3
"""
jarvis_model_pool — Connection pool for LLM model endpoints
Manages concurrent connections, enforces per-model limits, tracks utilization
"""

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.model_pool")

REDIS_PREFIX = "jarvis:pool:"


class PoolStatus(str, Enum):
    AVAILABLE = "available"
    FULL = "full"
    DEGRADED = "degraded"
    OFFLINE = "offline"


@dataclass
class PoolConfig:
    model: str
    endpoint_url: str
    max_connections: int = 4
    min_idle: int = 1
    connect_timeout_s: float = 5.0
    idle_timeout_s: float = 300.0
    max_retries: int = 2


@dataclass
class PoolConnection:
    conn_id: str
    model: str
    endpoint_url: str
    created_at: float = field(default_factory=time.time)
    acquired_at: float = 0.0
    released_at: float = 0.0
    request_count: int = 0
    error_count: int = 0

    @property
    def idle_duration(self) -> float:
        if self.released_at:
            return time.time() - self.released_at
        return 0.0

    @property
    def age_s(self) -> float:
        return time.time() - self.created_at

    @property
    def is_idle(self) -> bool:
        return self.acquired_at == 0.0 or self.released_at >= self.acquired_at

    def to_dict(self) -> dict:
        return {
            "conn_id": self.conn_id,
            "model": self.model,
            "is_idle": self.is_idle,
            "request_count": self.request_count,
            "error_count": self.error_count,
            "age_s": round(self.age_s, 1),
            "idle_s": round(self.idle_duration, 1),
        }


@dataclass
class PoolStats:
    model: str
    total_connections: int
    idle_connections: int
    active_connections: int
    total_requests: int
    total_errors: int
    wait_count: int
    avg_wait_ms: float

    @property
    def utilization_pct(self) -> float:
        if self.total_connections == 0:
            return 0.0
        return self.active_connections / self.total_connections * 100

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "total": self.total_connections,
            "idle": self.idle_connections,
            "active": self.active_connections,
            "utilization_pct": round(self.utilization_pct, 1),
            "total_requests": self.total_requests,
            "total_errors": self.total_errors,
            "avg_wait_ms": round(self.avg_wait_ms, 1),
        }


class ModelConnectionPool:
    def __init__(self, config: PoolConfig):
        self._config = config
        self._connections: dict[str, PoolConnection] = {}
        self._idle: asyncio.Queue = asyncio.Queue()
        self._semaphore = asyncio.Semaphore(config.max_connections)
        self._lock = asyncio.Lock()
        self._conn_counter = 0
        self._total_requests = 0
        self._total_errors = 0
        self._wait_times: list[float] = []
        self._status = PoolStatus.AVAILABLE

    @property
    def model(self) -> str:
        return self._config.model

    @property
    def status(self) -> PoolStatus:
        return self._status

    def _new_conn_id(self) -> str:
        self._conn_counter += 1
        return f"{self._config.model[:8]}-conn-{self._conn_counter:03d}"

    def _make_connection(self) -> PoolConnection:
        conn = PoolConnection(
            conn_id=self._new_conn_id(),
            model=self._config.model,
            endpoint_url=self._config.endpoint_url,
        )
        self._connections[conn.conn_id] = conn
        return conn

    @asynccontextmanager
    async def acquire(self, timeout_s: float = 30.0):
        """Acquire a connection from the pool. Yields the connection."""
        t_wait_start = time.time()
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=timeout_s)
        except asyncio.TimeoutError:
            raise RuntimeError(f"Pool {self.model}: acquire timeout after {timeout_s}s")

        wait_ms = (time.time() - t_wait_start) * 1000
        self._wait_times.append(wait_ms)
        if len(self._wait_times) > 200:
            self._wait_times.pop(0)

        # Get idle connection or create new one
        conn = None
        try:
            conn = self._idle.get_nowait()
            # Check if stale
            if conn.idle_duration > self._config.idle_timeout_s:
                del self._connections[conn.conn_id]
                conn = None
        except asyncio.QueueEmpty:
            pass

        if conn is None:
            conn = self._make_connection()

        conn.acquired_at = time.time()
        conn.request_count += 1
        self._total_requests += 1

        try:
            yield conn
        except Exception:
            conn.error_count += 1
            self._total_errors += 1
            raise
        finally:
            conn.released_at = time.time()
            self._semaphore.release()
            # Return to idle queue
            try:
                self._idle.put_nowait(conn)
            except asyncio.QueueFull:
                del self._connections[conn.conn_id]

    def active_count(self) -> int:
        return sum(1 for c in self._connections.values() if not c.is_idle)

    def idle_count(self) -> int:
        return self._idle.qsize()

    def pool_stats(self) -> PoolStats:
        avg_wait = sum(self._wait_times) / max(len(self._wait_times), 1)
        return PoolStats(
            model=self.model,
            total_connections=len(self._connections),
            idle_connections=self.idle_count(),
            active_connections=self.active_count(),
            total_requests=self._total_requests,
            total_errors=self._total_errors,
            wait_count=len(self._wait_times),
            avg_wait_ms=avg_wait,
        )


class ModelPool:
    """Manages pools for multiple model endpoints."""

    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._pools: dict[str, ModelConnectionPool] = {}
        self._stats: dict[str, int] = {"requests": 0, "errors": 0}

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def add_model(self, config: PoolConfig):
        self._pools[config.model] = ModelConnectionPool(config)
        log.info(f"Pool created for {config.model} (max={config.max_connections})")

    def get_pool(self, model: str) -> ModelConnectionPool | None:
        return self._pools.get(model)

    @asynccontextmanager
    async def acquire(self, model: str, timeout_s: float = 30.0):
        pool = self._pools.get(model)
        if not pool:
            raise ValueError(f"No pool for model: {model}")
        self._stats["requests"] += 1
        try:
            async with pool.acquire(timeout_s) as conn:
                yield conn
        except Exception:
            self._stats["errors"] += 1
            raise

    async def call(
        self,
        model: str,
        messages: list[dict],
        timeout_s: float = 30.0,
        **kwargs,
    ) -> dict:
        """Make an LLM call using the pool."""
        async with self.acquire(model, timeout_s) as conn:
            payload = {
                "model": model,
                "messages": messages,
                "max_tokens": kwargs.get("max_tokens", 1024),
                "temperature": kwargs.get("temperature", 0.7),
                **{
                    k: v
                    for k, v in kwargs.items()
                    if k not in ("max_tokens", "temperature")
                },
            }
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout_s)
            ) as sess:
                async with sess.post(
                    f"{conn.endpoint_url}/v1/chat/completions",
                    json=payload,
                ) as r:
                    if r.status == 200:
                        return await r.json(content_type=None)
                    text = await r.text()
                    raise RuntimeError(f"HTTP {r.status}: {text[:200]}")

    def all_stats(self) -> list[dict]:
        return [p.pool_stats().to_dict() for p in self._pools.values()]

    def stats(self) -> dict:
        return {**self._stats, "pools": len(self._pools)}


def build_jarvis_model_pool() -> ModelPool:
    pool = ModelPool()
    pool.add_model(
        PoolConfig("qwen3.5-9b", "http://192.168.1.85:1234", max_connections=6)
    )
    pool.add_model(
        PoolConfig("qwen3.5-27b", "http://192.168.1.85:1234", max_connections=3)
    )
    pool.add_model(
        PoolConfig("deepseek-r1-0528", "http://192.168.1.26:1234", max_connections=2)
    )
    pool.add_model(PoolConfig("gemma3:4b", "http://127.0.0.1:11434", max_connections=4))
    return pool


async def main():
    import sys

    pool = build_jarvis_model_pool()
    await pool.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Simulating concurrent pool acquisitions...")

        async def fake_request(model: str, req_id: int):
            try:
                async with pool.acquire(model, timeout_s=5.0) as conn:
                    await asyncio.sleep(0.05)  # simulate work
                    return f"req_{req_id} done on {conn.conn_id}"
            except Exception as e:
                return f"req_{req_id} failed: {e}"

        model = "qwen3.5-9b"
        tasks = [fake_request(model, i) for i in range(10)]
        results = await asyncio.gather(*tasks)
        for r in results:
            print(f"  {r}")

        print("\nPool stats:")
        for s in pool.all_stats():
            print(
                f"  {s['model']:<25} active={s['active']}/{s['total']} "
                f"reqs={s['total_requests']} util={s['utilization_pct']}% "
                f"avg_wait={s['avg_wait_ms']:.1f}ms"
            )

        print(f"\nStats: {json.dumps(pool.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(pool.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
