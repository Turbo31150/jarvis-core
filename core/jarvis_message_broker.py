#!/usr/bin/env python3
"""
jarvis_message_broker — In-process pub/sub message broker with topic routing
Topic-based message routing with wildcards, consumer groups, and delivery guarantees
"""

import asyncio
import fnmatch
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.message_broker")

REDIS_PREFIX = "jarvis:broker:"


@dataclass
class Message:
    msg_id: str
    topic: str
    payload: Any
    headers: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    ttl_s: float = 0.0  # 0 = no TTL
    priority: int = 5

    @property
    def expired(self) -> bool:
        return self.ttl_s > 0 and (time.time() - self.ts) > self.ttl_s

    def to_dict(self) -> dict:
        return {
            "msg_id": self.msg_id,
            "topic": self.topic,
            "payload": self.payload,
            "headers": self.headers,
            "ts": self.ts,
            "priority": self.priority,
        }


@dataclass
class Subscription:
    sub_id: str
    pattern: str  # topic pattern, supports * and ? wildcards
    handler: Callable
    group: str = "default"
    max_inflight: int = 100
    inflight: int = 0

    def matches(self, topic: str) -> bool:
        return fnmatch.fnmatch(topic, self.pattern)


@dataclass
class ConsumerGroup:
    name: str
    pattern: str
    queue: asyncio.Queue
    last_msg_id: str = ""
    delivered: int = 0
    acked: int = 0

    @property
    def pending(self) -> int:
        return self.delivered - self.acked


class MessageBroker:
    def __init__(self, max_retained: int = 1000):
        self.redis: aioredis.Redis | None = None
        self._subscriptions: dict[str, Subscription] = {}
        self._groups: dict[str, ConsumerGroup] = {}
        self._retained: dict[str, list[Message]] = {}  # topic → last N messages
        self._max_retained = max_retained
        self._stats: dict[str, int] = {
            "published": 0,
            "delivered": 0,
            "dropped": 0,
            "errors": 0,
        }
        self._dlq: list[Message] = []  # dead letter queue
        self._running = False
        self._task: asyncio.Task | None = None

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def subscribe(
        self,
        pattern: str,
        handler: Callable,
        sub_id: str | None = None,
        group: str = "default",
    ) -> str:
        sid = sub_id or str(uuid.uuid4())[:8]
        self._subscriptions[sid] = Subscription(
            sub_id=sid, pattern=pattern, handler=handler, group=group
        )
        log.debug(f"Subscribed [{sid}] to '{pattern}' (group={group})")
        return sid

    def unsubscribe(self, sub_id: str):
        self._subscriptions.pop(sub_id, None)

    def create_group(self, name: str, pattern: str) -> ConsumerGroup:
        q: asyncio.Queue = asyncio.Queue(maxsize=10000)
        group = ConsumerGroup(name=name, pattern=pattern, queue=q)
        self._groups[name] = group
        return group

    async def publish(
        self,
        topic: str,
        payload: Any,
        headers: dict | None = None,
        ttl_s: float = 0.0,
        priority: int = 5,
        retain: bool = False,
    ) -> str:
        msg = Message(
            msg_id=str(uuid.uuid4())[:10],
            topic=topic,
            payload=payload,
            headers=headers or {},
            ttl_s=ttl_s,
            priority=priority,
        )
        self._stats["published"] += 1

        # Retain last message per topic
        if retain:
            if topic not in self._retained:
                self._retained[topic] = []
            self._retained[topic].append(msg)
            self._retained[topic] = self._retained[topic][-self._max_retained :]

        # Fan-out to matching subscriptions
        matched = [s for s in self._subscriptions.values() if s.matches(topic)]
        for sub in matched:
            if sub.inflight >= sub.max_inflight:
                self._stats["dropped"] += 1
                log.warning(f"Subscription {sub.sub_id} backpressure, dropping msg")
                continue
            sub.inflight += 1
            asyncio.create_task(self._deliver(msg, sub))

        # Fan-out to consumer groups
        for group in self._groups.values():
            if fnmatch.fnmatch(topic, group.pattern):
                try:
                    group.queue.put_nowait(msg)
                    group.delivered += 1
                    group.last_msg_id = msg.msg_id
                except asyncio.QueueFull:
                    self._stats["dropped"] += 1

        # Push to Redis pub/sub
        if self.redis:
            asyncio.create_task(
                self.redis.publish(
                    f"{REDIS_PREFIX}topic:{topic}",
                    json.dumps(msg.to_dict()),
                )
            )

        return msg.msg_id

    async def _deliver(self, msg: Message, sub: Subscription):
        try:
            if msg.expired:
                self._stats["dropped"] += 1
                return
            if asyncio.iscoroutinefunction(sub.handler):
                await sub.handler(msg)
            else:
                sub.handler(msg)
            self._stats["delivered"] += 1
        except Exception as e:
            self._stats["errors"] += 1
            self._dlq.append(msg)
            log.error(f"Delivery error for {sub.sub_id}: {e}")
        finally:
            sub.inflight -= 1

    async def consume_group(
        self, group_name: str, timeout_s: float = 5.0
    ) -> Message | None:
        group = self._groups.get(group_name)
        if not group:
            return None
        try:
            msg = await asyncio.wait_for(group.queue.get(), timeout=timeout_s)
            return msg
        except asyncio.TimeoutError:
            return None

    def ack(self, group_name: str):
        group = self._groups.get(group_name)
        if group:
            group.acked += 1

    def get_retained(self, topic: str, n: int = 1) -> list[Message]:
        return self._retained.get(topic, [])[-n:]

    def topics(self) -> list[str]:
        return list(self._retained.keys())

    def stats(self) -> dict:
        return {
            **self._stats,
            "subscriptions": len(self._subscriptions),
            "groups": len(self._groups),
            "retained_topics": len(self._retained),
            "dlq_size": len(self._dlq),
        }


async def main():
    import sys

    broker = MessageBroker()
    await broker.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        received = []

        async def on_inference(msg: Message):
            received.append(f"inference:{msg.payload.get('model', '?')}")

        async def on_all(msg: Message):
            received.append(f"all:{msg.topic}")

        broker.subscribe("jarvis.inference.*", on_inference)
        broker.subscribe("jarvis.*", on_all)

        group = broker.create_group("workers", "jarvis.tasks.*")

        await broker.publish(
            "jarvis.inference.request", {"model": "qwen3.5-9b", "prompt": "hi"}
        )
        await broker.publish(
            "jarvis.inference.response", {"model": "qwen3.5-9b", "result": "hello"}
        )
        await broker.publish("jarvis.tasks.process", {"task_id": "abc"})
        await broker.publish("jarvis.system.health", {"status": "ok"}, retain=True)

        await asyncio.sleep(0.05)

        task_msg = await broker.consume_group("workers", timeout_s=0.5)
        broker.ack("workers")

        print(f"Received: {received}")
        print(f"Task from group: {task_msg.payload if task_msg else None}")
        retained = broker.get_retained("jarvis.system.health")
        print(f"Retained health: {retained[0].payload if retained else None}")
        print(f"\nStats: {json.dumps(broker.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(broker.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
