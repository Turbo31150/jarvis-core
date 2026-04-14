#!/usr/bin/env python3
"""
jarvis_cluster_sync — State synchronization across cluster nodes
CRDTs, vector clocks, conflict resolution, and Redis-backed distributed state
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.cluster_sync")

REDIS_PREFIX = "jarvis:sync:"
REDIS_CHANNEL = "jarvis:sync:updates"


class ConflictStrategy(str, Enum):
    LAST_WRITE_WINS = "lww"
    FIRST_WRITE_WINS = "fww"
    MERGE = "merge"  # merge dicts / union sets
    MAX_VALUE = "max"  # for numeric values
    CUSTOM = "custom"


class SyncKind(str, Enum):
    FULL = "full"
    DELTA = "delta"
    TOMBSTONE = "tombstone"  # deletion marker


@dataclass
class VectorClock:
    clocks: dict[str, int] = field(default_factory=dict)

    def tick(self, node_id: str) -> "VectorClock":
        new = VectorClock(dict(self.clocks))
        new.clocks[node_id] = new.clocks.get(node_id, 0) + 1
        return new

    def merge(self, other: "VectorClock") -> "VectorClock":
        all_nodes = set(self.clocks) | set(other.clocks)
        merged = {
            n: max(self.clocks.get(n, 0), other.clocks.get(n, 0)) for n in all_nodes
        }
        return VectorClock(merged)

    def happens_before(self, other: "VectorClock") -> bool:
        """Returns True if self < other (self happened before other)."""
        all_nodes = set(self.clocks) | set(other.clocks)
        dominated = False
        for n in all_nodes:
            sv = self.clocks.get(n, 0)
            ov = other.clocks.get(n, 0)
            if sv > ov:
                return False
            if sv < ov:
                dominated = True
        return dominated

    def concurrent_with(self, other: "VectorClock") -> bool:
        return not self.happens_before(other) and not other.happens_before(self)

    def to_dict(self) -> dict:
        return dict(self.clocks)

    @classmethod
    def from_dict(cls, d: dict) -> "VectorClock":
        return cls(clocks={k: int(v) for k, v in d.items()})


@dataclass
class SyncEntry:
    key: str
    value: Any
    node_id: str
    vector_clock: VectorClock
    ts: float = field(default_factory=time.time)
    kind: SyncKind = SyncKind.DELTA
    checksum: str = ""

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "value": self.value,
            "node_id": self.node_id,
            "vector_clock": self.vector_clock.to_dict(),
            "ts": self.ts,
            "kind": self.kind.value,
        }


@dataclass
class SyncConflict:
    key: str
    local: SyncEntry
    remote: SyncEntry
    resolved_value: Any = None
    resolution: str = ""


class ClusterSync:
    def __init__(
        self,
        node_id: str,
        conflict_strategy: ConflictStrategy = ConflictStrategy.LAST_WRITE_WINS,
    ):
        self.redis: aioredis.Redis | None = None
        self._node_id = node_id
        self._strategy = conflict_strategy
        self._state: dict[str, SyncEntry] = {}
        self._vector_clock = VectorClock()
        self._conflicts: list[SyncConflict] = []
        self._peers: set[str] = set()
        self._stats: dict[str, int] = {
            "writes": 0,
            "reads": 0,
            "syncs": 0,
            "conflicts_detected": 0,
            "conflicts_resolved": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def set(self, key: str, value: Any) -> SyncEntry:
        self._vector_clock = self._vector_clock.tick(self._node_id)
        entry = SyncEntry(
            key=key,
            value=value,
            node_id=self._node_id,
            vector_clock=VectorClock(dict(self._vector_clock.clocks)),
        )
        self._state[key] = entry
        self._stats["writes"] += 1

        if self.redis:
            asyncio.create_task(self._redis_publish(entry))

        return entry

    def delete(self, key: str):
        self._vector_clock = self._vector_clock.tick(self._node_id)
        tombstone = SyncEntry(
            key=key,
            value=None,
            node_id=self._node_id,
            vector_clock=VectorClock(dict(self._vector_clock.clocks)),
            kind=SyncKind.TOMBSTONE,
        )
        self._state[key] = tombstone
        if self.redis:
            asyncio.create_task(self._redis_publish(tombstone))

    def get(self, key: str) -> Any:
        self._stats["reads"] += 1
        entry = self._state.get(key)
        if entry is None or entry.kind == SyncKind.TOMBSTONE:
            return None
        return entry.value

    def _resolve_conflict(self, local: SyncEntry, remote: SyncEntry) -> SyncEntry:
        conflict = SyncConflict(local.key, local, remote)
        self._stats["conflicts_detected"] += 1

        if self._strategy == ConflictStrategy.LAST_WRITE_WINS:
            winner = remote if remote.ts > local.ts else local
            conflict.resolved_value = winner.value
            conflict.resolution = f"lww: {'remote' if winner is remote else 'local'}"
        elif self._strategy == ConflictStrategy.FIRST_WRITE_WINS:
            winner = local if local.ts <= remote.ts else remote
            conflict.resolved_value = winner.value
            conflict.resolution = f"fww: {'local' if winner is local else 'remote'}"
        elif self._strategy == ConflictStrategy.MAX_VALUE:
            try:
                winner_val = max(local.value, remote.value)
                conflict.resolved_value = winner_val
                conflict.resolution = "max_value"
            except TypeError:
                conflict.resolved_value = remote.value
                conflict.resolution = "max_value:type_error→remote"
        elif self._strategy == ConflictStrategy.MERGE:
            if isinstance(local.value, dict) and isinstance(remote.value, dict):
                merged = {**local.value, **remote.value}
                conflict.resolved_value = merged
                conflict.resolution = "merge:dicts"
            elif isinstance(local.value, list) and isinstance(remote.value, list):
                merged_list = list(dict.fromkeys(local.value + remote.value))
                conflict.resolved_value = merged_list
                conflict.resolution = "merge:lists"
            else:
                conflict.resolved_value = remote.value
                conflict.resolution = "merge:fallback_remote"
        else:
            conflict.resolved_value = remote.value
            conflict.resolution = "default_remote"

        self._conflicts.append(conflict)
        self._stats["conflicts_resolved"] += 1

        resolved = SyncEntry(
            key=local.key,
            value=conflict.resolved_value,
            node_id=self._node_id,
            vector_clock=local.vector_clock.merge(remote.vector_clock),
            ts=max(local.ts, remote.ts),
        )
        log.info(f"Conflict resolved for '{local.key}': {conflict.resolution}")
        return resolved

    def receive(self, entry: SyncEntry):
        """Apply a remote sync entry."""
        self._stats["syncs"] += 1
        local = self._state.get(entry.key)

        if local is None:
            self._state[entry.key] = entry
            self._vector_clock = self._vector_clock.merge(entry.vector_clock)
            return

        if local.vector_clock.happens_before(entry.vector_clock):
            # Remote is strictly newer
            self._state[entry.key] = entry
            self._vector_clock = self._vector_clock.merge(entry.vector_clock)
        elif entry.vector_clock.happens_before(local.vector_clock):
            # Local is strictly newer — ignore remote
            pass
        else:
            # Concurrent — need conflict resolution
            resolved = self._resolve_conflict(local, entry)
            self._state[entry.key] = resolved
            self._vector_clock = self._vector_clock.merge(entry.vector_clock)

    async def sync_from_redis(self):
        if not self.redis:
            return
        try:
            keys = await self.redis.keys(f"{REDIS_PREFIX}state:{self._node_id}:*")
            for key in keys:
                raw = await self.redis.get(key)
                if raw:
                    d = json.loads(raw)
                    entry = SyncEntry(
                        key=d["key"],
                        value=d["value"],
                        node_id=d["node_id"],
                        vector_clock=VectorClock.from_dict(d["vector_clock"]),
                        ts=d["ts"],
                        kind=SyncKind(d.get("kind", "delta")),
                    )
                    self.receive(entry)
        except Exception as e:
            log.warning(f"Redis sync error: {e}")

    async def _redis_publish(self, entry: SyncEntry):
        if not self.redis:
            return
        try:
            payload = json.dumps(entry.to_dict())
            await self.redis.publish(REDIS_CHANNEL, payload)
            key = f"{REDIS_PREFIX}state:{self._node_id}:{entry.key}"
            await self.redis.setex(key, 3600, payload)
        except Exception as e:
            log.warning(f"Redis publish error: {e}")

    def full_state(self) -> dict:
        return {
            k: e.value for k, e in self._state.items() if e.kind != SyncKind.TOMBSTONE
        }

    def conflict_log(self) -> list[dict]:
        return [
            {
                "key": c.key,
                "resolution": c.resolution,
                "resolved_value": c.resolved_value,
            }
            for c in self._conflicts
        ]

    def stats(self) -> dict:
        return {
            **self._stats,
            "state_size": len(self._state),
            "vector_clock": self._vector_clock.to_dict(),
            "node_id": self._node_id,
            "strategy": self._strategy.value,
        }


def build_jarvis_cluster_sync(node_id: str = "jarvis-core") -> ClusterSync:
    return ClusterSync(
        node_id=node_id,
        conflict_strategy=ConflictStrategy.LAST_WRITE_WINS,
    )


async def main():
    import sys

    node_a = build_jarvis_cluster_sync("m1")
    node_b = build_jarvis_cluster_sync("m2")
    await node_a.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Simulating distributed sync...")

        # Node A writes
        node_a.set("config.timeout", 30)
        node_a.set("model.active", "qwen3.5-9b")
        node_a.set("cluster.leader", "m1")

        # Node B writes (concurrent)
        node_b.set("config.timeout", 45)  # conflict!
        node_b.set("model.active", "deepseek-r1")  # conflict!
        node_b.set("workers.count", 4)

        print("\nNode A state (before sync):", node_a.full_state())
        print("Node B state (before sync):", node_b.full_state())

        # Sync: A receives B's entries
        for key, entry in node_b._state.items():
            node_a.receive(entry)

        # Sync: B receives A's entries
        for key, entry in node_a._state.items():
            node_b.receive(entry)

        print("\nNode A state (after sync):", node_a.full_state())
        print("Node B state (after sync):", node_b.full_state())

        print("\nConflicts on A:")
        for c in node_a.conflict_log():
            print(f"  [{c['key']}] {c['resolution']} → {c['resolved_value']}")

        print(f"\nNode A stats: {json.dumps(node_a.stats(), indent=2)}")

    elif cmd == "stats":
        node = build_jarvis_cluster_sync("local")
        print(json.dumps(node.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
