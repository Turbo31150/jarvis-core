#!/usr/bin/env python3
"""
jarvis_cluster_replicator — State replication across cluster nodes via Redis pub/sub
Keeps in-memory state synchronized between M1/M2/OL1 with conflict resolution
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.cluster_replicator")

REDIS_CHANNEL = "jarvis:replication"
REDIS_STATE_KEY = "jarvis:replicated_state"


class ConflictPolicy(str, Enum):
    LAST_WRITE_WINS = "lww"
    HIGHEST_VERSION = "version"
    LEADER_WINS = "leader"


@dataclass
class StateEntry:
    key: str
    value: Any
    version: int
    node_id: str
    ts: float = field(default_factory=time.time)
    ttl_s: float = 0.0

    @property
    def expired(self) -> bool:
        return self.ttl_s > 0 and (time.time() - self.ts) > self.ttl_s

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "value": self.value,
            "version": self.version,
            "node_id": self.node_id,
            "ts": self.ts,
            "ttl_s": self.ttl_s,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "StateEntry":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class ReplicationMessage:
    msg_id: str
    op: str  # set | delete | sync_request | sync_response
    node_id: str
    entries: list[StateEntry]
    ts: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps(
            {
                "msg_id": self.msg_id,
                "op": self.op,
                "node_id": self.node_id,
                "entries": [e.to_dict() for e in self.entries],
                "ts": self.ts,
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> "ReplicationMessage":
        d = json.loads(raw)
        entries = [StateEntry.from_dict(e) for e in d.get("entries", [])]
        return cls(
            msg_id=d["msg_id"],
            op=d["op"],
            node_id=d["node_id"],
            entries=entries,
            ts=d["ts"],
        )


class ClusterReplicator:
    def __init__(
        self,
        node_id: str,
        conflict_policy: ConflictPolicy = ConflictPolicy.LAST_WRITE_WINS,
        leader_id: str = "",
    ):
        self.node_id = node_id
        self._conflict_policy = conflict_policy
        self._leader_id = leader_id
        self.redis: aioredis.Redis | None = None
        self._pubsub: aioredis.client.PubSub | None = None
        self._state: dict[str, StateEntry] = {}
        self._version_counter: int = 0
        self._running = False
        self._change_callbacks: list = []
        self._stats: dict[str, int] = {
            "sets": 0,
            "deletes": 0,
            "received": 0,
            "conflicts_resolved": 0,
            "syncs": 0,
        }

    async def connect(self):
        self.redis = aioredis.Redis(decode_responses=True)
        await self.redis.ping()
        self._pubsub = self.redis.pubsub()
        await self._pubsub.subscribe(REDIS_CHANNEL)
        log.info(f"Replicator [{self.node_id}] connected, channel={REDIS_CHANNEL}")

    def on_change(self, callback):
        self._change_callbacks.append(callback)

    def _next_version(self) -> int:
        self._version_counter += 1
        return self._version_counter

    def _resolve_conflict(
        self, existing: StateEntry, incoming: StateEntry
    ) -> StateEntry:
        if self._conflict_policy == ConflictPolicy.LAST_WRITE_WINS:
            winner = incoming if incoming.ts >= existing.ts else existing
        elif self._conflict_policy == ConflictPolicy.HIGHEST_VERSION:
            winner = incoming if incoming.version > existing.version else existing
        elif self._conflict_policy == ConflictPolicy.LEADER_WINS:
            winner = incoming if incoming.node_id == self._leader_id else existing
        else:
            winner = incoming

        if winner is not existing:
            self._stats["conflicts_resolved"] += 1
        return winner

    async def set(self, key: str, value: Any, ttl_s: float = 0.0):
        entry = StateEntry(
            key=key,
            value=value,
            version=self._next_version(),
            node_id=self.node_id,
            ttl_s=ttl_s,
        )
        self._state[key] = entry
        self._stats["sets"] += 1

        if self.redis:
            msg = ReplicationMessage(
                msg_id=str(uuid.uuid4())[:8],
                op="set",
                node_id=self.node_id,
                entries=[entry],
            )
            await self.redis.publish(REDIS_CHANNEL, msg.to_json())
            # Also persist to hash
            await self.redis.hset(REDIS_STATE_KEY, key, json.dumps(entry.to_dict()))

    async def delete(self, key: str):
        self._state.pop(key, None)
        self._stats["deletes"] += 1
        if self.redis:
            msg = ReplicationMessage(
                msg_id=str(uuid.uuid4())[:8],
                op="delete",
                node_id=self.node_id,
                entries=[
                    StateEntry(
                        key=key,
                        value=None,
                        version=self._next_version(),
                        node_id=self.node_id,
                    )
                ],
            )
            await self.redis.publish(REDIS_CHANNEL, msg.to_json())
            await self.redis.hdel(REDIS_STATE_KEY, key)

    def get(self, key: str, default: Any = None) -> Any:
        entry = self._state.get(key)
        if entry is None or entry.expired:
            return default
        return entry.value

    def _apply_message(self, msg: ReplicationMessage):
        if msg.node_id == self.node_id:
            return  # ignore own messages
        self._stats["received"] += 1

        for entry in msg.entries:
            if msg.op == "set":
                existing = self._state.get(entry.key)
                if existing:
                    winner = self._resolve_conflict(existing, entry)
                    self._state[entry.key] = winner
                else:
                    self._state[entry.key] = entry
                for cb in self._change_callbacks:
                    try:
                        cb("set", entry.key, entry.value)
                    except Exception:
                        pass

            elif msg.op == "delete":
                self._state.pop(entry.key, None)
                for cb in self._change_callbacks:
                    try:
                        cb("delete", entry.key, None)
                    except Exception:
                        pass

            elif msg.op == "sync_response":
                self._state[entry.key] = entry

    async def request_sync(self):
        """Ask peers to send their full state."""
        if not self.redis:
            return
        self._stats["syncs"] += 1
        msg = ReplicationMessage(
            msg_id=str(uuid.uuid4())[:8],
            op="sync_request",
            node_id=self.node_id,
            entries=[],
        )
        await self.redis.publish(REDIS_CHANNEL, msg.to_json())

    async def _handle_sync_request(self):
        """Respond to sync requests with our full state."""
        if not self.redis:
            return
        entries = [e for e in self._state.values() if not e.expired]
        if not entries:
            return
        msg = ReplicationMessage(
            msg_id=str(uuid.uuid4())[:8],
            op="sync_response",
            node_id=self.node_id,
            entries=entries,
        )
        await self.redis.publish(REDIS_CHANNEL, msg.to_json())

    async def _prune_expired(self):
        expired_keys = [k for k, e in self._state.items() if e.expired]
        for k in expired_keys:
            del self._state[k]

    async def run(self):
        self._running = True
        prune_interval = 60.0
        last_prune = time.time()

        async for raw_msg in self._pubsub.listen():
            if not self._running:
                break
            if raw_msg["type"] != "message":
                continue
            try:
                msg = ReplicationMessage.from_json(raw_msg["data"])
                if msg.op == "sync_request" and msg.node_id != self.node_id:
                    await self._handle_sync_request()
                else:
                    self._apply_message(msg)
            except Exception as e:
                log.debug(f"Replication parse error: {e}")

            if time.time() - last_prune > prune_interval:
                await self._prune_expired()
                last_prune = time.time()

    def stop(self):
        self._running = False

    def local_state(self) -> dict[str, Any]:
        return {k: e.value for k, e in self._state.items() if not e.expired}

    def stats(self) -> dict:
        return {**self._stats, "keys": len(self._state), "node_id": self.node_id}


async def main():
    import sys

    node_id = sys.argv[2] if len(sys.argv) > 2 else "m1"
    replicator = ClusterReplicator(node_id=node_id)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    try:
        await replicator.connect()
    except Exception as e:
        print(f"Redis unavailable: {e}")
        return

    if cmd == "demo":

        def on_change(op, key, value):
            print(f"  [CHANGE] {op} {key}={value}")

        replicator.on_change(on_change)

        await replicator.set("cluster.active_model", "qwen3.5-9b")
        await replicator.set("cluster.gpu_temp", 65.0, ttl_s=300)
        await replicator.set("cluster.inference_count", 1042)
        print(f"Local state: {replicator.local_state()}")
        print(f"Stats: {json.dumps(replicator.stats(), indent=2)}")

    elif cmd == "listen":
        print(f"[{node_id}] Listening for replication messages...")
        await replicator.request_sync()
        await asyncio.sleep(5)
        print(f"State: {replicator.local_state()}")

    elif cmd == "stats":
        print(json.dumps(replicator.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
