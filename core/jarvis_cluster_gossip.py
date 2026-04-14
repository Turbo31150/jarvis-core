#!/usr/bin/env python3
"""
jarvis_cluster_gossip — Gossip protocol for cluster membership and state sharing
Nodes exchange state via Redis pub/sub with anti-entropy and convergence detection
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

log = logging.getLogger("jarvis.cluster_gossip")

REDIS_PREFIX = "jarvis:gossip:"
GOSSIP_CHANNEL = "jarvis:gossip:messages"
HEARTBEAT_INTERVAL_S = 10.0
DEAD_TIMEOUT_S = 45.0  # node is dead if no heartbeat for this long


class NodeStatus(str, Enum):
    ALIVE = "alive"
    SUSPECT = "suspect"
    DEAD = "dead"


@dataclass
class NodeState:
    node_id: str
    host: str
    port: int
    status: NodeStatus = NodeStatus.ALIVE
    generation: int = 0  # incremented on restart (used to detect stale state)
    heartbeat_seq: int = 0  # monotonically increasing
    metadata: dict = field(default_factory=dict)
    last_seen: float = field(default_factory=time.time)

    @property
    def age_s(self) -> float:
        return time.time() - self.last_seen

    @property
    def stale(self) -> bool:
        return self.age_s > DEAD_TIMEOUT_S

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "host": self.host,
            "port": self.port,
            "status": self.status.value,
            "generation": self.generation,
            "heartbeat_seq": self.heartbeat_seq,
            "metadata": self.metadata,
            "last_seen": self.last_seen,
            "age_s": round(self.age_s, 1),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "NodeState":
        d = dict(d)
        d["status"] = NodeStatus(d.get("status", "alive"))
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class GossipMessage:
    msg_type: str  # heartbeat | state_sync | join | leave | update
    sender_id: str
    payload: dict
    ts: float = field(default_factory=time.time)
    msg_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])

    def to_json(self) -> str:
        return json.dumps(
            {
                "msg_type": self.msg_type,
                "sender_id": self.sender_id,
                "payload": self.payload,
                "ts": self.ts,
                "msg_id": self.msg_id,
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> "GossipMessage":
        d = json.loads(raw)
        return cls(**d)


class ClusterGossip:
    def __init__(
        self,
        node_id: str | None = None,
        host: str = "localhost",
        port: int = 0,
        metadata: dict | None = None,
    ):
        self.redis: aioredis.Redis | None = None
        self.node_id = node_id or str(uuid.uuid4())[:10]
        self.host = host
        self.port = port
        self.metadata = metadata or {}
        self._nodes: dict[str, NodeState] = {}
        self._generation = int(time.time())
        self._heartbeat_seq = 0
        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._on_join: list[Any] = []
        self._on_leave: list[Any] = []
        self._stats: dict[str, int] = {
            "heartbeats_sent": 0,
            "heartbeats_recv": 0,
            "joins": 0,
            "leaves": 0,
            "state_syncs": 0,
        }

    async def connect_redis(self):
        self.redis = aioredis.Redis(decode_responses=True)
        await self.redis.ping()

    def _self_state(self) -> NodeState:
        return NodeState(
            node_id=self.node_id,
            host=self.host,
            port=self.port,
            status=NodeStatus.ALIVE,
            generation=self._generation,
            heartbeat_seq=self._heartbeat_seq,
            metadata=self.metadata,
            last_seen=time.time(),
        )

    async def _publish(self, msg: GossipMessage):
        if not self.redis:
            return
        await self.redis.publish(GOSSIP_CHANNEL, msg.to_json())

    async def _heartbeat_loop(self):
        while self._running:
            self._heartbeat_seq += 1
            state = self._self_state()
            self._nodes[self.node_id] = state

            # Store in Redis hash for new joiners
            if self.redis:
                await self.redis.hset(
                    f"{REDIS_PREFIX}members",
                    self.node_id,
                    json.dumps(state.to_dict()),
                )
                await self.redis.expire(
                    f"{REDIS_PREFIX}members", int(DEAD_TIMEOUT_S * 2)
                )

            msg = GossipMessage(
                msg_type="heartbeat",
                sender_id=self.node_id,
                payload=state.to_dict(),
            )
            await self._publish(msg)
            self._stats["heartbeats_sent"] += 1

            # Detect suspect/dead nodes
            now = time.time()
            for nid, ns in list(self._nodes.items()):
                if nid == self.node_id:
                    continue
                if ns.age_s > DEAD_TIMEOUT_S and ns.status != NodeStatus.DEAD:
                    ns.status = NodeStatus.DEAD
                    log.warning(
                        f"Node {nid} declared DEAD (last seen {ns.age_s:.0f}s ago)"
                    )
                    for cb in self._on_leave:
                        try:
                            cb(nid, ns)
                        except Exception:
                            pass
                elif ns.age_s > DEAD_TIMEOUT_S / 2 and ns.status == NodeStatus.ALIVE:
                    ns.status = NodeStatus.SUSPECT
                    log.debug(f"Node {nid} SUSPECT")

            await asyncio.sleep(HEARTBEAT_INTERVAL_S)

    async def _subscribe_loop(self):
        if not self.redis:
            return
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(GOSSIP_CHANNEL)
        async for raw_msg in pubsub.listen():
            if not self._running:
                break
            if raw_msg["type"] != "message":
                continue
            try:
                msg = GossipMessage.from_json(raw_msg["data"])
                if msg.sender_id == self.node_id:
                    continue  # ignore own messages
                await self._handle_message(msg)
            except Exception as e:
                log.debug(f"Gossip parse error: {e}")

    async def _handle_message(self, msg: GossipMessage):
        if msg.msg_type == "heartbeat":
            self._stats["heartbeats_recv"] += 1
            state = NodeState.from_dict(msg.payload)
            existing = self._nodes.get(msg.sender_id)

            # Accept if higher generation or heartbeat seq
            if not existing or (
                state.generation > existing.generation
                or (
                    state.generation == existing.generation
                    and state.heartbeat_seq > existing.heartbeat_seq
                )
            ):
                is_new = (
                    msg.sender_id not in self._nodes
                    or self._nodes[msg.sender_id].status == NodeStatus.DEAD
                )
                self._nodes[msg.sender_id] = state
                if is_new:
                    self._stats["joins"] += 1
                    log.info(
                        f"Node joined: {msg.sender_id} @ {state.host}:{state.port}"
                    )
                    for cb in self._on_join:
                        try:
                            cb(msg.sender_id, state)
                        except Exception:
                            pass

        elif msg.msg_type == "state_sync":
            self._stats["state_syncs"] += 1
            for nid, nd in msg.payload.items():
                if nid not in self._nodes:
                    self._nodes[nid] = NodeState.from_dict(nd)

        elif msg.msg_type == "leave":
            nid = msg.payload.get("node_id")
            if nid and nid in self._nodes:
                self._nodes[nid].status = NodeStatus.DEAD
                self._stats["leaves"] += 1

    async def join(self):
        """Announce joining, load existing members."""
        if not self.redis:
            return
        # Load existing members
        raw = await self.redis.hgetall(f"{REDIS_PREFIX}members")
        for nid, blob in raw.items():
            if nid != self.node_id:
                try:
                    self._nodes[nid] = NodeState.from_dict(json.loads(blob))
                except Exception:
                    pass

        msg = GossipMessage(
            msg_type="join",
            sender_id=self.node_id,
            payload=self._self_state().to_dict(),
        )
        await self._publish(msg)
        log.info(
            f"Joined cluster as {self.node_id} (found {len(self._nodes)} existing members)"
        )

    async def leave(self):
        msg = GossipMessage(
            msg_type="leave",
            sender_id=self.node_id,
            payload={"node_id": self.node_id},
        )
        await self._publish(msg)
        if self.redis:
            await self.redis.hdel(f"{REDIS_PREFIX}members", self.node_id)

    def on_join(self, callback):
        self._on_join.append(callback)

    def on_leave(self, callback):
        self._on_leave.append(callback)

    async def start(self):
        self._running = True
        await self.join()
        self._tasks.append(asyncio.create_task(self._heartbeat_loop()))
        self._tasks.append(asyncio.create_task(self._subscribe_loop()))

    async def stop(self):
        self._running = False
        await self.leave()
        for t in self._tasks:
            t.cancel()

    def members(self) -> list[dict]:
        return [
            ns.to_dict() for ns in self._nodes.values() if ns.status != NodeStatus.DEAD
        ]

    def alive_nodes(self) -> list[str]:
        return [nid for nid, ns in self._nodes.items() if ns.status == NodeStatus.ALIVE]

    def stats(self) -> dict:
        statuses = {s.value: 0 for s in NodeStatus}
        for ns in self._nodes.values():
            statuses[ns.status.value] += 1
        return {
            **self._stats,
            "node_id": self.node_id,
            "known_nodes": len(self._nodes),
            "status_counts": statuses,
        }


async def main():
    import sys

    node = ClusterGossip(
        node_id="m1-jarvis",
        host="192.168.1.85",
        port=1234,
        metadata={"role": "inference", "gpus": 3},
    )
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    try:
        await node.connect_redis()
    except Exception as e:
        print(f"Redis unavailable: {e}")
        return

    if cmd == "demo":
        joins = []
        node.on_join(lambda nid, ns: joins.append(nid))

        # Simulate a second node joining
        node2 = ClusterGossip(
            node_id="m2-jarvis",
            host="192.168.1.26",
            port=1234,
            metadata={"role": "inference", "gpus": 1},
        )
        await node2.connect_redis()

        await node.start()
        await asyncio.sleep(0.5)
        await node2.start()
        await asyncio.sleep(1.0)

        print(f"Node1 members: {[m['node_id'] for m in node.members()]}")
        print(f"Node2 members: {[m['node_id'] for m in node2.members()]}")
        print(f"Joins detected: {joins}")

        await node2.stop()
        await node.stop()
        print(f"\nStats: {json.dumps(node.stats(), indent=2)}")

    elif cmd == "members":
        await node.join()
        await asyncio.sleep(0.5)
        for m in node.members():
            print(f"  {m['node_id']:<20} {m['status']:<10} {m['host']}:{m['port']}")

    elif cmd == "stats":
        print(json.dumps(node.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
