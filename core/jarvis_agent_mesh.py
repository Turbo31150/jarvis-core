#!/usr/bin/env python3
"""
jarvis_agent_mesh — Peer-to-peer agent communication mesh
Agents discover each other, exchange capabilities, and route tasks directly
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.agent_mesh")

REDIS_PREFIX = "jarvis:mesh:"
REDIS_CHANNEL = "jarvis:mesh:broadcast"
HEARTBEAT_TTL = 30.0


class MessageType(str, Enum):
    HELLO = "hello"  # agent announces itself
    BYE = "bye"  # agent leaving
    PING = "ping"
    PONG = "pong"
    TASK = "task"  # delegate task to peer
    RESULT = "result"  # task result
    BROADCAST = "broadcast"  # all agents
    QUERY = "query"  # ask for info
    RESPONSE = "response"


@dataclass
class AgentPeer:
    agent_id: str
    name: str
    capabilities: list[str] = field(default_factory=list)
    endpoint: str = ""  # direct address if available
    load: float = 0.0  # 0.0–1.0
    last_seen: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)

    @property
    def alive(self) -> bool:
        return (time.time() - self.last_seen) < HEARTBEAT_TTL

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "capabilities": self.capabilities,
            "endpoint": self.endpoint,
            "load": round(self.load, 2),
            "last_seen": self.last_seen,
            "alive": self.alive,
        }


@dataclass
class MeshMessage:
    msg_id: str
    msg_type: MessageType
    sender_id: str
    recipient_id: str = ""  # empty = broadcast
    payload: Any = None
    ts: float = field(default_factory=time.time)
    correlation_id: str = ""
    ttl_hops: int = 5

    def to_dict(self) -> dict:
        return {
            "msg_id": self.msg_id,
            "msg_type": self.msg_type.value,
            "sender_id": self.sender_id,
            "recipient_id": self.recipient_id,
            "payload": self.payload,
            "ts": self.ts,
            "correlation_id": self.correlation_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MeshMessage":
        return cls(
            msg_id=d["msg_id"],
            msg_type=MessageType(d["msg_type"]),
            sender_id=d["sender_id"],
            recipient_id=d.get("recipient_id", ""),
            payload=d.get("payload"),
            ts=d.get("ts", time.time()),
            correlation_id=d.get("correlation_id", ""),
        )


class AgentMesh:
    def __init__(
        self, agent_id: str, agent_name: str, capabilities: list[str] | None = None
    ):
        self.redis: aioredis.Redis | None = None
        self._agent_id = agent_id
        self._agent_name = agent_name
        self._capabilities = capabilities or []
        self._peers: dict[str, AgentPeer] = {}
        self._handlers: dict[MessageType, list[Callable]] = {}
        self._pending: dict[str, asyncio.Future] = {}  # correlation_id → future
        self._listen_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._stats: dict[str, int] = {
            "sent": 0,
            "received": 0,
            "tasks_delegated": 0,
            "tasks_received": 0,
            "peers_discovered": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def on(self, msg_type: MessageType, handler: Callable):
        self._handlers.setdefault(msg_type, []).append(handler)

    async def _dispatch(self, msg: MeshMessage):
        for handler in self._handlers.get(msg.msg_type, []):
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(msg)
                else:
                    handler(msg)
            except Exception as e:
                log.warning(f"Mesh handler error: {e}")

        # Resolve pending futures
        if msg.correlation_id and msg.correlation_id in self._pending:
            fut = self._pending.pop(msg.correlation_id)
            if not fut.done():
                fut.set_result(msg)

    async def _send(self, msg: MeshMessage):
        if not self.redis:
            return
        self._stats["sent"] += 1
        payload = json.dumps(msg.to_dict())
        try:
            if msg.recipient_id:
                # Point-to-point via Redis key
                key = f"{REDIS_PREFIX}inbox:{msg.recipient_id}"
                await self.redis.lpush(key, payload)
                await self.redis.expire(key, 60)
            else:
                # Broadcast via pub/sub
                await self.redis.publish(REDIS_CHANNEL, payload)
        except Exception as e:
            log.warning(f"Mesh send error: {e}")

    async def send(
        self,
        recipient_id: str,
        msg_type: MessageType,
        payload: Any = None,
        wait_reply: bool = False,
        timeout_s: float = 10.0,
    ) -> MeshMessage | None:
        corr_id = str(uuid.uuid4())[:8]
        msg = MeshMessage(
            msg_id=str(uuid.uuid4())[:8],
            msg_type=msg_type,
            sender_id=self._agent_id,
            recipient_id=recipient_id,
            payload=payload,
            correlation_id=corr_id,
        )
        if wait_reply:
            fut: asyncio.Future = asyncio.get_event_loop().create_future()
            self._pending[corr_id] = fut
        await self._send(msg)
        if wait_reply:
            try:
                return await asyncio.wait_for(fut, timeout=timeout_s)
            except asyncio.TimeoutError:
                self._pending.pop(corr_id, None)
                return None
        return None

    async def broadcast(self, msg_type: MessageType, payload: Any = None):
        msg = MeshMessage(
            msg_id=str(uuid.uuid4())[:8],
            msg_type=msg_type,
            sender_id=self._agent_id,
            payload=payload,
        )
        await self._send(msg)

    async def delegate_task(
        self,
        task: Any,
        capability: str | None = None,
        timeout_s: float = 30.0,
    ) -> Any | None:
        """Delegate task to the most suitable peer."""
        peer = self._find_peer(capability)
        if not peer:
            log.warning(f"No peer found for capability '{capability}'")
            return None
        self._stats["tasks_delegated"] += 1
        reply = await self.send(
            peer.agent_id,
            MessageType.TASK,
            {"task": task, "capability": capability},
            wait_reply=True,
            timeout_s=timeout_s,
        )
        return reply.payload if reply else None

    def _find_peer(self, capability: str | None = None) -> AgentPeer | None:
        alive = [
            p for p in self._peers.values() if p.alive and p.agent_id != self._agent_id
        ]
        if capability:
            alive = [p for p in alive if capability in p.capabilities]
        if not alive:
            return None
        return min(alive, key=lambda p: p.load)

    async def join(self):
        """Announce ourselves to the mesh."""
        await self.broadcast(
            MessageType.HELLO,
            {
                "agent_id": self._agent_id,
                "name": self._agent_name,
                "capabilities": self._capabilities,
            },
        )
        # Register in Redis
        if self.redis:
            peer_data = json.dumps(
                {
                    "agent_id": self._agent_id,
                    "name": self._agent_name,
                    "capabilities": self._capabilities,
                    "ts": time.time(),
                }
            )
            await self.redis.setex(
                f"{REDIS_PREFIX}peer:{self._agent_id}",
                int(HEARTBEAT_TTL * 2),
                peer_data,
            )

    async def leave(self):
        await self.broadcast(MessageType.BYE, {"agent_id": self._agent_id})
        if self._listen_task:
            self._listen_task.cancel()
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self.redis:
            await self.redis.delete(f"{REDIS_PREFIX}peer:{self._agent_id}")

    async def _process_inbox(self):
        if not self.redis:
            return
        key = f"{REDIS_PREFIX}inbox:{self._agent_id}"
        try:
            raw = await self.redis.rpop(key)
            while raw:
                msg = MeshMessage.from_dict(json.loads(raw))
                self._stats["received"] += 1
                await self._dispatch(msg)
                raw = await self.redis.rpop(key)
        except Exception as e:
            log.warning(f"Inbox error: {e}")

    def _handle_hello(self, msg: MeshMessage):
        p = msg.payload or {}
        peer = AgentPeer(
            agent_id=p.get("agent_id", msg.sender_id),
            name=p.get("name", ""),
            capabilities=p.get("capabilities", []),
        )
        if peer.agent_id not in self._peers:
            self._stats["peers_discovered"] += 1
        self._peers[peer.agent_id] = peer
        log.debug(f"Peer joined mesh: {peer.agent_id} ({peer.name})")

    def _handle_bye(self, msg: MeshMessage):
        p = msg.payload or {}
        aid = p.get("agent_id", msg.sender_id)
        self._peers.pop(aid, None)
        log.debug(f"Peer left mesh: {aid}")

    async def _heartbeat_loop(self):
        while True:
            await asyncio.sleep(HEARTBEAT_TTL / 3)
            await self.join()
            await self._process_inbox()

    async def start(self):
        self.on(MessageType.HELLO, self._handle_hello)
        self.on(MessageType.BYE, self._handle_bye)
        await self.join()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    def live_peers(self) -> list[dict]:
        return [p.to_dict() for p in self._peers.values() if p.alive]

    def stats(self) -> dict:
        return {
            **self._stats,
            "live_peers": len([p for p in self._peers.values() if p.alive]),
            "agent_id": self._agent_id,
            "capabilities": self._capabilities,
        }


def build_jarvis_agent_mesh(
    agent_id: str = "jarvis-core",
    capabilities: list[str] | None = None,
) -> AgentMesh:
    return AgentMesh(
        agent_id=agent_id,
        agent_name="JARVIS Core",
        capabilities=capabilities or ["inference", "routing", "monitoring"],
    )


async def main():
    import sys

    agent_a = build_jarvis_agent_mesh("agent-a", ["inference", "embed"])
    agent_b = build_jarvis_agent_mesh("agent-b", ["trading", "analysis"])
    await agent_a.connect_redis()
    await agent_b.connect_redis()

    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        # Register task handler on B
        async def on_task_b(msg: MeshMessage):
            print(f"  Agent B received task: {msg.payload}")
            # Reply
            await agent_b.send(
                msg.sender_id,
                MessageType.RESULT,
                {"result": "analysis_done", "input": msg.payload},
                wait_reply=False,
            )

        agent_b.on(MessageType.TASK, on_task_b)
        await agent_b.start()
        await agent_a.start()

        # Give time for hello messages
        await asyncio.sleep(0.1)
        await agent_a._process_inbox()
        await agent_b._process_inbox()

        print("Live peers on A:", [p["agent_id"] for p in agent_a.live_peers()])
        print("Live peers on B:", [p["agent_id"] for p in agent_b.live_peers()])

        # A delegates to B
        await agent_a.send("agent-b", MessageType.TASK, {"analysis": "BTC/USDT"})
        await asyncio.sleep(0.1)
        await agent_b._process_inbox()

        print(f"\nAgent A stats: {json.dumps(agent_a.stats(), indent=2)}")
        await agent_a.leave()
        await agent_b.leave()

    elif cmd == "stats":
        mesh = build_jarvis_agent_mesh()
        await mesh.connect_redis()
        print(json.dumps(mesh.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
