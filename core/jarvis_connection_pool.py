#!/usr/bin/env python3
"""
jarvis_connection_pool — Async HTTP connection pool with health checking
Manages persistent connections to cluster nodes with circuit breaking and failover
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.connection_pool")

REDIS_PREFIX = "jarvis:connpool:"


class NodeState(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"
    UNKNOWN = "unknown"


@dataclass
class NodeConfig:
    name: str
    base_url: str
    timeout_s: float = 10.0
    max_connections: int = 20
    health_path: str = "/health"
    weight: int = 10  # for weighted routing


@dataclass
class NodeStats:
    name: str
    state: NodeState = NodeState.UNKNOWN
    total_requests: int = 0
    errors: int = 0
    latency_ms_ema: float = 0.0
    consecutive_errors: int = 0
    last_check: float = 0.0
    last_error: str = ""

    @property
    def error_rate(self) -> float:
        return self.errors / max(self.total_requests, 1)

    @property
    def healthy(self) -> bool:
        return self.state == NodeState.HEALTHY

    def record(self, ok: bool, latency_ms: float, error: str = ""):
        self.total_requests += 1
        alpha = 0.1
        self.latency_ms_ema = alpha * latency_ms + (1 - alpha) * self.latency_ms_ema
        if ok:
            self.consecutive_errors = 0
            self.state = NodeState.HEALTHY
        else:
            self.errors += 1
            self.consecutive_errors += 1
            self.last_error = error[:100]
            if self.consecutive_errors >= 3:
                self.state = NodeState.DOWN
            elif self.consecutive_errors >= 1:
                self.state = NodeState.DEGRADED

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "state": self.state.value,
            "total_requests": self.total_requests,
            "errors": self.errors,
            "error_rate": round(self.error_rate * 100, 1),
            "latency_ms": round(self.latency_ms_ema, 1),
            "consecutive_errors": self.consecutive_errors,
            "last_error": self.last_error,
        }


class ConnectionPool:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._nodes: dict[str, NodeConfig] = {}
        self._stats: dict[str, NodeStats] = {}
        self._sessions: dict[str, aiohttp.ClientSession] = {}
        self._health_task: asyncio.Task | None = None
        self._running = False

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def add_node(self, config: NodeConfig):
        self._nodes[config.name] = config
        self._stats[config.name] = NodeStats(name=config.name)
        log.debug(f"Node added: {config.name} @ {config.base_url}")

    def _get_session(self, name: str) -> aiohttp.ClientSession:
        if name not in self._sessions or self._sessions[name].closed:
            cfg = self._nodes[name]
            connector = aiohttp.TCPConnector(
                limit=cfg.max_connections, limit_per_host=cfg.max_connections
            )
            self._sessions[name] = aiohttp.ClientSession(
                base_url=cfg.base_url,
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=cfg.timeout_s),
            )
        return self._sessions[name]

    async def request(
        self,
        node_name: str,
        method: str,
        path: str,
        **kwargs,
    ) -> tuple[int, Any]:
        cfg = self._nodes.get(node_name)
        stats = self._stats.get(node_name)
        if not cfg or not stats:
            raise KeyError(f"Unknown node: {node_name}")

        if stats.state == NodeState.DOWN:
            raise ConnectionError(f"Node '{node_name}' is DOWN")

        session = self._get_session(node_name)
        t0 = time.time()
        try:
            async with session.request(method, path, **kwargs) as resp:
                latency = (time.time() - t0) * 1000
                ok = resp.status < 500
                stats.record(ok, latency)
                body = await resp.json(content_type=None)
                return resp.status, body
        except Exception as e:
            latency = (time.time() - t0) * 1000
            stats.record(False, latency, str(e))
            raise

    async def get(self, node_name: str, path: str, **kwargs) -> tuple[int, Any]:
        return await self.request(node_name, "GET", path, **kwargs)

    async def post(self, node_name: str, path: str, **kwargs) -> tuple[int, Any]:
        return await self.request(node_name, "POST", path, **kwargs)

    def best_node(self, exclude: set[str] | None = None) -> str | None:
        """Pick healthiest node by lowest latency."""
        exclude = exclude or set()
        candidates = [
            (name, s)
            for name, s in self._stats.items()
            if name not in exclude and s.state != NodeState.DOWN
        ]
        if not candidates:
            return None

        # Weight by node config weight / latency
        def score(name: str, s: NodeStats) -> float:
            weight = self._nodes[name].weight
            lat = max(s.latency_ms_ema, 1.0)
            return weight / lat

        return max(candidates, key=lambda x: score(x[0], x[1]))[0]

    async def request_any(
        self, method: str, path: str, **kwargs
    ) -> tuple[str, int, Any]:
        """Try nodes in order of health until one succeeds."""
        tried: set[str] = set()
        while True:
            name = self.best_node(exclude=tried)
            if not name:
                raise ConnectionError("All nodes failed or unavailable")
            try:
                status, body = await self.request(name, method, path, **kwargs)
                return name, status, body
            except Exception as e:
                log.warning(f"Node {name} failed: {e}, trying next")
                tried.add(name)

    async def health_check_node(self, name: str) -> bool:
        cfg = self._nodes[name]
        stats = self._stats[name]
        try:
            session = self._get_session(name)
            t0 = time.time()
            async with session.get(
                cfg.health_path, timeout=aiohttp.ClientTimeout(total=3)
            ) as r:
                ok = r.status < 400
                stats.record(ok, (time.time() - t0) * 1000)
                stats.last_check = time.time()
                return ok
        except Exception as e:
            stats.record(False, 3000, str(e))
            stats.last_check = time.time()
            return False

    async def _health_loop(self, interval_s: float = 30.0):
        while self._running:
            for name in list(self._nodes.keys()):
                await self.health_check_node(name)
            await asyncio.sleep(interval_s)

    async def start_health_checks(self, interval_s: float = 30.0):
        self._running = True
        self._health_task = asyncio.create_task(self._health_loop(interval_s))

    async def stop(self):
        self._running = False
        if self._health_task:
            self._health_task.cancel()
        for sess in self._sessions.values():
            if not sess.closed:
                await sess.close()

    def node_stats(self) -> list[dict]:
        return [s.to_dict() for s in self._stats.values()]

    def stats(self) -> dict:
        healthy = sum(1 for s in self._stats.values() if s.healthy)
        return {
            "nodes": len(self._nodes),
            "healthy": healthy,
            "down": sum(1 for s in self._stats.values() if s.state == NodeState.DOWN),
            "total_requests": sum(s.total_requests for s in self._stats.values()),
            "total_errors": sum(s.errors for s in self._stats.values()),
        }


def build_jarvis_pool() -> ConnectionPool:
    pool = ConnectionPool()
    pool.add_node(NodeConfig("m1", "http://192.168.1.85:1234", weight=10))
    pool.add_node(NodeConfig("m2", "http://192.168.1.26:1234", weight=8))
    pool.add_node(
        NodeConfig("ol1", "http://127.0.0.1:11434", weight=5, health_path="/api/tags")
    )
    return pool


async def main():
    import sys

    pool = build_jarvis_pool()
    await pool.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Checking node health...")
        for name in ["m1", "m2", "ol1"]:
            ok = await pool.health_check_node(name)
            stats = pool._stats[name]
            status = "✅" if ok else "❌"
            print(
                f"  {status} {name}: {stats.state.value} ({stats.latency_ms_ema:.0f}ms)"
            )

        best = pool.best_node()
        print(f"\nBest node: {best}")
        print(f"Stats: {json.dumps(pool.stats(), indent=2)}")
        await pool.stop()

    elif cmd == "health":
        for name in pool._nodes:
            ok = await pool.health_check_node(name)
            print(f"  {name}: {'ok' if ok else 'down'}")
        await pool.stop()

    elif cmd == "stats":
        print(json.dumps(pool.stats(), indent=2))
        await pool.stop()


if __name__ == "__main__":
    asyncio.run(main())
