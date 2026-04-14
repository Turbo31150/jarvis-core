#!/usr/bin/env python3
"""
jarvis_gossip_protocol — Gossip-based cluster state dissemination
Node membership, rumor spreading, eventual consistency for cluster topology
"""

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.gossip_protocol")

REDIS_PREFIX = "jarvis:gossip:"
GOSSIP_CHANNEL = "jarvis:gossip:broadcast"


class NodeStatus(str, Enum):
    ALIVE = "alive"
    SUSPECT = "suspect"
    DEAD = "dead"
    LEFT = "left"


@dataclass
class NodeInfo:
    node_id: str
    address: str
    status: NodeStatus = NodeStatus.ALIVE
    version: int = 0  # lamport-style incarnation number
    last_seen: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)
    suspect_since: float = 0.0

    @property
    def age_s(self) -> float:
        return time.time() - self.last_seen

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "address": self.address,
            "status": self.status.value,
            "version": self.version,
            "last_seen": self.last_seen,
            "age_s": round(self.age_s, 1),
            "metadata": self.metadata,
        }


@dataclass
class GossipMessage:
    origin: str
    msg_type: str  # "heartbeat" | "state" | "join" | "leave" | "suspect"
    nodes: list[dict]
    ts: float = field(default_factory=time.time)
    ttl: int = 3  # hops remaining

    def to_dict(self) -> dict:
        return {
            "origin": self.origin,
            "type": self.msg_type,
            "nodes": self.nodes,
            "ts": self.ts,
            "ttl": self.ttl,
        }


class GossipProtocol:
    def __init__(
        self,
        node_id: str,
        address: str,
        fanout: int = 3,
        suspect_timeout_s: float = 10.0,
        dead_timeout_s: float = 30.0,
        gossip_interval_s: float = 1.0,
    ):
        self.redis: aioredis.Redis | None = None
        self._node_id = node_id
        self._address = address
        self._fanout = fanout
        self._suspect_timeout = suspect_timeout_s
        self._dead_timeout = dead_timeout_s
        self._gossip_interval = gossip_interval_s

        self._members: dict[str, NodeInfo] = {}
        self._seen_msgs: set[str] = set()
        self._max_seen = 10_000

        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._change_callbacks: list = []

        self._stats: dict[str, int] = {
            "msgs_sent": 0,
            "msgs_received": 0,
            "msgs_dropped": 0,
            "nodes_joined": 0,
            "nodes_left": 0,
            "nodes_suspected": 0,
            "nodes_declared_dead": 0,
        }

        # Register self
        self._members[node_id] = NodeInfo(node_id=node_id, address=address)

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def on_change(self, callback):
        self._change_callbacks.append(callback)

    def _notify(self, event: str, node: NodeInfo):
        for cb in self._change_callbacks:
            try:
                cb(event, node)
            except Exception:
                pass

    def join(self, node_id: str, address: str, metadata: dict | None = None):
        existing = self._members.get(node_id)
        if existing:
            existing.status = NodeStatus.ALIVE
            existing.last_seen = time.time()
            existing.version += 1
            if metadata:
                existing.metadata.update(metadata)
            return

        node = NodeInfo(node_id=node_id, address=address, metadata=metadata or {})
        self._members[node_id] = node
        self._stats["nodes_joined"] += 1
        self._notify("join", node)
        log.info(f"Node joined: {node_id!r} @ {address}")

    def leave(self, node_id: str):
        node = self._members.get(node_id)
        if node:
            node.status = NodeStatus.LEFT
            self._stats["nodes_left"] += 1
            self._notify("leave", node)
            log.info(f"Node left: {node_id!r}")

    def _make_msg_key(self, origin: str, ts: float) -> str:
        return f"{origin}:{ts:.3f}"

    def _is_seen(self, key: str) -> bool:
        if key in self._seen_msgs:
            return True
        self._seen_msgs.add(key)
        if len(self._seen_msgs) > self._max_seen:
            to_remove = list(self._seen_msgs)[: self._max_seen // 2]
            for k in to_remove:
                self._seen_msgs.discard(k)
        return False

    def _merge_nodes(self, nodes: list[dict]):
        for nd in nodes:
            node_id = nd.get("node_id", "")
            if not node_id or node_id == self._node_id:
                continue
            status = NodeStatus(nd.get("status", "alive"))
            version = nd.get("version", 0)
            existing = self._members.get(node_id)

            if existing is None:
                node = NodeInfo(
                    node_id=node_id,
                    address=nd.get("address", ""),
                    status=status,
                    version=version,
                    last_seen=nd.get("last_seen", time.time()),
                    metadata=nd.get("metadata", {}),
                )
                self._members[node_id] = node
                self._stats["nodes_joined"] += 1
                self._notify("join", node)
            elif version > existing.version or status != existing.status:
                existing.status = status
                existing.version = version
                existing.last_seen = nd.get("last_seen", existing.last_seen)
                existing.metadata.update(nd.get("metadata", {}))
                self._notify("update", existing)

    async def _spread(self, msg: GossipMessage):
        if not self.redis:
            return
        if msg.ttl <= 0:
            return
        msg.ttl -= 1
        try:
            await self.redis.publish(GOSSIP_CHANNEL, json.dumps(msg.to_dict()))
            self._stats["msgs_sent"] += 1
        except Exception as e:
            log.debug(f"Gossip spread error: {e}")

    async def _gossip_loop(self):
        while self._running:
            await asyncio.sleep(self._gossip_interval)
            # Update own heartbeat
            me = self._members[self._node_id]
            me.last_seen = time.time()
            me.version += 1

            # Pick random subset of alive members to include
            alive = [n for n in self._members.values() if n.status == NodeStatus.ALIVE]
            sample = random.sample(alive, min(self._fanout * 2, len(alive)))

            msg = GossipMessage(
                origin=self._node_id,
                msg_type="heartbeat",
                nodes=[n.to_dict() for n in sample],
            )
            await self._spread(msg)

    async def _failure_detector_loop(self):
        while self._running:
            await asyncio.sleep(self._gossip_interval * 2)
            now = time.time()
            for node_id, node in list(self._members.items()):
                if node_id == self._node_id:
                    continue
                if node.status == NodeStatus.LEFT:
                    continue

                if (
                    node.status == NodeStatus.ALIVE
                    and node.age_s > self._suspect_timeout
                ):
                    node.status = NodeStatus.SUSPECT
                    node.suspect_since = now
                    self._stats["nodes_suspected"] += 1
                    self._notify("suspect", node)
                    log.warning(f"Node {node_id!r} is suspect (age={node.age_s:.1f}s)")

                elif node.status == NodeStatus.SUSPECT:
                    suspect_age = now - node.suspect_since
                    if suspect_age > self._dead_timeout:
                        node.status = NodeStatus.DEAD
                        self._stats["nodes_declared_dead"] += 1
                        self._notify("dead", node)
                        log.error(f"Node {node_id!r} declared DEAD")

    async def _listen_loop(self):
        if not self.redis:
            return
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(GOSSIP_CHANNEL)
        async for message in pubsub.listen():
            if not self._running:
                break
            if message["type"] != "message":
                continue
            try:
                data = json.loads(message["data"])
                origin = data.get("origin", "")
                ts = data.get("ts", 0.0)
                key = self._make_msg_key(origin, ts)
                if self._is_seen(key):
                    self._stats["msgs_dropped"] += 1
                    continue

                self._stats["msgs_received"] += 1
                self._merge_nodes(data.get("nodes", []))

                # Re-spread if TTL allows
                msg = GossipMessage(
                    origin=origin,
                    msg_type=data.get("type", "heartbeat"),
                    nodes=data.get("nodes", []),
                    ts=ts,
                    ttl=data.get("ttl", 0),
                )
                if msg.ttl > 0:
                    await self._spread(msg)

            except Exception as e:
                log.debug(f"Gossip listen error: {e}")

    def start(self):
        self._running = True
        self._tasks = [
            asyncio.create_task(self._gossip_loop()),
            asyncio.create_task(self._failure_detector_loop()),
        ]
        if self.redis:
            self._tasks.append(asyncio.create_task(self._listen_loop()))
        log.info(f"GossipProtocol started for node {self._node_id!r}")

    async def stop(self):
        self._running = False
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def alive_members(self) -> list[NodeInfo]:
        return [n for n in self._members.values() if n.status == NodeStatus.ALIVE]

    def get_member(self, node_id: str) -> NodeInfo | None:
        return self._members.get(node_id)

    def membership(self) -> list[dict]:
        return [n.to_dict() for n in self._members.values()]

    def stats(self) -> dict:
        by_status: dict[str, int] = {}
        for n in self._members.values():
            by_status[n.status.value] = by_status.get(n.status.value, 0) + 1
        return {
            **self._stats,
            "members": len(self._members),
            "by_status": by_status,
        }


def build_jarvis_gossip_protocol(
    node_id: str = "m1", address: str = "192.168.1.85:7946"
) -> GossipProtocol:
    gp = GossipProtocol(
        node_id=node_id,
        address=address,
        fanout=3,
        suspect_timeout_s=10.0,
        dead_timeout_s=30.0,
        gossip_interval_s=1.0,
    )
    # Seed known cluster members
    gp.join("m2", "192.168.1.26:7946")
    gp.join("ol1", "127.0.0.1:7946")
    return gp


async def main():
    import sys

    node_id = sys.argv[2] if len(sys.argv) > 2 else "m1"
    gp = build_jarvis_gossip_protocol(node_id=node_id)
    await gp.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print(f"Gossip protocol demo (node={node_id!r})...")

        # Simulate node updates
        gp.join("m3", "192.168.1.133:7946", {"role": "gpu", "gpus": 2})

        # Simulate m2 going suspect
        m2 = gp.get_member("m2")
        if m2:
            m2.last_seen = time.time() - 60
            m2.status = NodeStatus.SUSPECT

        print("\nMembership:")
        for m in gp.membership():
            icon = {"alive": "✅", "suspect": "⚠️", "dead": "❌", "left": "👋"}.get(
                m["status"], "?"
            )
            print(
                f"  {icon} {m['node_id']:<8} {m['address']:<25} age={m['age_s']:.0f}s"
            )

        print(f"\nAlive members: {[n.node_id for n in gp.alive_members()]}")
        print(f"\nStats: {json.dumps(gp.stats(), indent=2)}")

    elif cmd == "members":
        for m in gp.membership():
            print(f"  {m['node_id']:<8} {m['status']:<10} {m['address']}")

    elif cmd == "stats":
        print(json.dumps(gp.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
