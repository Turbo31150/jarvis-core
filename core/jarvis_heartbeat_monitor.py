#!/usr/bin/env python3
"""
jarvis_heartbeat_monitor — Heartbeat emission and liveness tracking for cluster nodes
Configurable intervals, miss thresholds, automatic dead-node detection
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.heartbeat_monitor")

REDIS_PREFIX = "jarvis:hb:"
HB_CHANNEL = "jarvis:heartbeat"


class NodeHealth(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    DEAD = "dead"
    UNKNOWN = "unknown"


@dataclass
class HeartbeatRecord:
    node_id: str
    address: str
    interval_s: float = 5.0
    miss_threshold: int = 3  # misses before UNHEALTHY
    dead_threshold: int = 6  # misses before DEAD
    last_beat: float = 0.0
    miss_count: int = 0
    health: NodeHealth = NodeHealth.UNKNOWN
    metadata: dict[str, Any] = field(default_factory=dict)
    total_beats: int = 0
    total_misses: int = 0

    @property
    def age_s(self) -> float:
        return time.time() - self.last_beat if self.last_beat else float("inf")

    @property
    def expected_misses(self) -> int:
        if self.last_beat == 0.0:
            return 0
        return int(self.age_s / self.interval_s)

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "address": self.address,
            "health": self.health.value,
            "last_beat": self.last_beat,
            "age_s": round(self.age_s, 1),
            "miss_count": self.miss_count,
            "total_beats": self.total_beats,
            "total_misses": self.total_misses,
            "metadata": self.metadata,
        }


class HeartbeatMonitor:
    def __init__(
        self,
        node_id: str,
        emit_interval_s: float = 5.0,
        check_interval_s: float = 2.5,
    ):
        self.redis: aioredis.Redis | None = None
        self._node_id = node_id
        self._emit_interval = emit_interval_s
        self._check_interval = check_interval_s

        self._records: dict[str, HeartbeatRecord] = {}
        self._callbacks: dict[str, list[Callable]] = {}  # health → [cb]
        self._running = False
        self._tasks: list[asyncio.Task] = []

        self._stats: dict[str, int] = {
            "beats_emitted": 0,
            "beats_received": 0,
            "nodes_degraded": 0,
            "nodes_unhealthy": 0,
            "nodes_dead": 0,
            "nodes_recovered": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def watch(
        self,
        node_id: str,
        address: str,
        interval_s: float = 5.0,
        miss_threshold: int = 3,
        dead_threshold: int = 6,
        metadata: dict | None = None,
    ):
        self._records[node_id] = HeartbeatRecord(
            node_id=node_id,
            address=address,
            interval_s=interval_s,
            miss_threshold=miss_threshold,
            dead_threshold=dead_threshold,
            metadata=metadata or {},
        )

    def on_health_change(self, health: NodeHealth, callback: Callable):
        self._callbacks.setdefault(health.value, []).append(callback)

    def _fire(self, health: NodeHealth, record: HeartbeatRecord):
        for cb in self._callbacks.get(health.value, []):
            try:
                cb(record)
            except Exception:
                pass

    def receive_beat(self, node_id: str, metadata: dict | None = None):
        record = self._records.get(node_id)
        if not record:
            return
        now = time.time()
        prev_health = record.health
        record.last_beat = now
        record.total_beats += 1
        record.miss_count = 0
        record.health = NodeHealth.HEALTHY
        if metadata:
            record.metadata.update(metadata)
        self._stats["beats_received"] += 1

        if prev_health not in (NodeHealth.HEALTHY, NodeHealth.UNKNOWN):
            self._stats["nodes_recovered"] += 1
            self._fire(NodeHealth.HEALTHY, record)
            log.info(f"Node {node_id!r} recovered → HEALTHY")

        if self.redis:
            asyncio.create_task(self._redis_store(record))

    async def _redis_store(self, record: HeartbeatRecord):
        if not self.redis:
            return
        try:
            await self.redis.setex(
                f"{REDIS_PREFIX}{record.node_id}",
                int(record.interval_s * record.dead_threshold * 2),
                json.dumps(record.to_dict()),
            )
        except Exception:
            pass

    async def _emit_loop(self):
        while self._running:
            await asyncio.sleep(self._emit_interval)
            beat = {
                "node_id": self._node_id,
                "ts": time.time(),
                "type": "heartbeat",
            }
            if self.redis:
                try:
                    await self.redis.publish(HB_CHANNEL, json.dumps(beat))
                    self._stats["beats_emitted"] += 1
                except Exception:
                    pass

            # Also store own TTL key
            if self.redis:
                try:
                    await self.redis.setex(
                        f"{REDIS_PREFIX}{self._node_id}",
                        int(self._emit_interval * 6),
                        json.dumps(
                            {
                                "node_id": self._node_id,
                                "ts": time.time(),
                                "health": "healthy",
                            }
                        ),
                    )
                except Exception:
                    pass

    async def _check_loop(self):
        while self._running:
            await asyncio.sleep(self._check_interval)
            now = time.time()
            for node_id, record in self._records.items():
                if record.last_beat == 0.0:
                    continue

                expected_misses = int(record.age_s / record.interval_s)
                if expected_misses <= 0:
                    continue

                new_misses = expected_misses - record.miss_count
                if new_misses <= 0:
                    continue

                record.miss_count = expected_misses
                record.total_misses += new_misses
                prev = record.health

                if record.miss_count >= record.dead_threshold:
                    record.health = NodeHealth.DEAD
                    if prev != NodeHealth.DEAD:
                        self._stats["nodes_dead"] += 1
                        self._fire(NodeHealth.DEAD, record)
                        log.error(f"Node {node_id!r} DEAD (misses={record.miss_count})")
                elif record.miss_count >= record.miss_threshold:
                    record.health = NodeHealth.UNHEALTHY
                    if prev not in (NodeHealth.UNHEALTHY, NodeHealth.DEAD):
                        self._stats["nodes_unhealthy"] += 1
                        self._fire(NodeHealth.UNHEALTHY, record)
                        log.warning(
                            f"Node {node_id!r} UNHEALTHY (misses={record.miss_count})"
                        )
                elif record.miss_count >= 1:
                    record.health = NodeHealth.DEGRADED
                    if prev == NodeHealth.HEALTHY:
                        self._stats["nodes_degraded"] += 1
                        self._fire(NodeHealth.DEGRADED, record)

    async def _listen_loop(self):
        if not self.redis:
            return
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(HB_CHANNEL)
        async for message in pubsub.listen():
            if not self._running:
                break
            if message["type"] != "message":
                continue
            try:
                data = json.loads(message["data"])
                node_id = data.get("node_id", "")
                if node_id and node_id in self._records:
                    self.receive_beat(node_id)
            except Exception:
                pass

    def start(self):
        self._running = True
        self._tasks = [
            asyncio.create_task(self._emit_loop()),
            asyncio.create_task(self._check_loop()),
        ]
        if self.redis:
            self._tasks.append(asyncio.create_task(self._listen_loop()))
        log.info(f"HeartbeatMonitor started for {self._node_id!r}")

    async def stop(self):
        self._running = False
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def health_of(self, node_id: str) -> NodeHealth:
        return self._records.get(node_id, HeartbeatRecord("?", "?")).health

    def all_healthy(self) -> bool:
        return all(r.health == NodeHealth.HEALTHY for r in self._records.values())

    def unhealthy_nodes(self) -> list[str]:
        return [
            nid
            for nid, r in self._records.items()
            if r.health in (NodeHealth.UNHEALTHY, NodeHealth.DEAD)
        ]

    def snapshot(self) -> list[dict]:
        return [r.to_dict() for r in self._records.values()]

    def stats(self) -> dict:
        by_health: dict[str, int] = {}
        for r in self._records.values():
            by_health[r.health.value] = by_health.get(r.health.value, 0) + 1
        return {
            **self._stats,
            "watched_nodes": len(self._records),
            "by_health": by_health,
        }


def build_jarvis_heartbeat_monitor(node_id: str = "m1") -> HeartbeatMonitor:
    mon = HeartbeatMonitor(node_id=node_id, emit_interval_s=5.0, check_interval_s=2.5)
    mon.watch("m1", "192.168.1.85:7946", interval_s=5.0)
    mon.watch("m2", "192.168.1.26:7946", interval_s=5.0)
    mon.watch("ol1", "127.0.0.1:7946", interval_s=10.0)

    def on_dead(record: HeartbeatRecord):
        log.critical(f"ALERT: Node {record.node_id!r} is DEAD — failover required")

    mon.on_health_change(NodeHealth.DEAD, on_dead)
    return mon


async def main():
    import sys

    mon = build_jarvis_heartbeat_monitor()
    await mon.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Heartbeat monitor demo...")

        # Simulate beats
        mon.receive_beat("m1", {"cpu": 45, "ram": 60})
        mon.receive_beat("m2", {"cpu": 30, "ram": 40})
        # ol1 never beats → will be UNKNOWN

        print("\nNode health snapshot:")
        for r in mon.snapshot():
            icon = {
                "healthy": "✅",
                "degraded": "⚠️",
                "unhealthy": "🔴",
                "dead": "💀",
                "unknown": "❓",
            }.get(r["health"], "?")
            print(
                f"  {icon} {r['node_id']:<8} {r['health']:<12} last_beat_age={r['age_s']:.1f}s misses={r['miss_count']}"
            )

        print(f"\n  All healthy: {mon.all_healthy()}")
        print(f"  Unhealthy:   {mon.unhealthy_nodes()}")
        print(f"\nStats: {json.dumps(mon.stats(), indent=2)}")

    elif cmd == "snapshot":
        print(json.dumps(mon.snapshot(), indent=2))

    elif cmd == "stats":
        print(json.dumps(mon.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
