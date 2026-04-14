#!/usr/bin/env python3
"""
jarvis_message_bus — In-process and distributed message bus with topic routing
Async pub/sub with wildcards, message filtering, dead-letter queue, replay
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.message_bus")

DLQ_FILE = Path("/home/turbo/IA/Core/jarvis/data/message_dlq.jsonl")
REDIS_STREAM = "jarvis:msgbus:stream"
REDIS_PREFIX = "jarvis:msgbus:"


class MessagePriority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


_PRIORITY_ORDER = {
    MessagePriority.LOW: 0,
    MessagePriority.NORMAL: 1,
    MessagePriority.HIGH: 2,
    MessagePriority.CRITICAL: 3,
}


@dataclass
class Message:
    msg_id: str
    topic: str
    payload: Any
    priority: MessagePriority = MessagePriority.NORMAL
    source: str = ""
    correlation_id: str = ""
    reply_to: str = ""
    ttl_s: float = 0.0  # 0 = no expiry
    created_at: float = field(default_factory=time.time)
    attempts: int = 0
    headers: dict = field(default_factory=dict)

    @property
    def expired(self) -> bool:
        return self.ttl_s > 0 and (time.time() - self.created_at) > self.ttl_s

    def to_dict(self) -> dict:
        return {
            "msg_id": self.msg_id,
            "topic": self.topic,
            "payload": self.payload,
            "priority": self.priority.value,
            "source": self.source,
            "correlation_id": self.correlation_id,
            "created_at": self.created_at,
            "attempts": self.attempts,
            "headers": self.headers,
        }


@dataclass
class Subscription:
    sub_id: str
    topic_pattern: str  # supports "*" wildcard suffix e.g. "cluster.*"
    handler: Callable  # async or sync
    filter_fn: Callable | None = None  # optional msg filter
    max_concurrent: int = 4
    auto_ack: bool = True
    _semaphore: asyncio.Semaphore = field(init=False, repr=False)

    def __post_init__(self):
        self._semaphore = asyncio.Semaphore(self.max_concurrent)

    def matches(self, topic: str) -> bool:
        if self.topic_pattern == "*":
            return True
        if self.topic_pattern.endswith(".*"):
            prefix = self.topic_pattern[:-2]
            return topic == prefix or topic.startswith(prefix + ".")
        return self.topic_pattern == topic


@dataclass
class BusStats:
    published: int = 0
    delivered: int = 0
    dropped: int = 0
    dlq_sent: int = 0
    handler_errors: int = 0
    subscriptions: int = 0


class MessageBus:
    def __init__(
        self,
        dlq_enabled: bool = True,
        max_history: int = 500,
    ):
        self.redis: aioredis.Redis | None = None
        self._subscriptions: dict[str, Subscription] = {}
        self._dlq: list[Message] = []
        self._history: list[Message] = []
        self._max_history = max_history
        self._dlq_enabled = dlq_enabled
        self._stats = BusStats()
        if dlq_enabled:
            DLQ_FILE.parent.mkdir(parents=True, exist_ok=True)

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def subscribe(
        self,
        topic_pattern: str,
        handler: Callable,
        filter_fn: Callable | None = None,
        max_concurrent: int = 4,
    ) -> str:
        sub_id = str(uuid.uuid4())[:8]
        sub = Subscription(
            sub_id=sub_id,
            topic_pattern=topic_pattern,
            handler=handler,
            filter_fn=filter_fn,
            max_concurrent=max_concurrent,
        )
        self._subscriptions[sub_id] = sub
        self._stats.subscriptions += 1
        log.debug(f"Subscribed {sub_id} to '{topic_pattern}'")
        return sub_id

    def unsubscribe(self, sub_id: str) -> bool:
        if sub_id in self._subscriptions:
            del self._subscriptions[sub_id]
            self._stats.subscriptions -= 1
            return True
        return False

    async def publish(
        self,
        topic: str,
        payload: Any,
        priority: MessagePriority = MessagePriority.NORMAL,
        source: str = "",
        ttl_s: float = 0.0,
        correlation_id: str = "",
        headers: dict | None = None,
    ) -> str:
        msg = Message(
            msg_id=str(uuid.uuid4())[:12],
            topic=topic,
            payload=payload,
            priority=priority,
            source=source,
            ttl_s=ttl_s,
            correlation_id=correlation_id,
            headers=headers or {},
        )
        self._stats.published += 1

        # History
        self._history.append(msg)
        if len(self._history) > self._max_history:
            self._history.pop(0)

        # Redis stream (async, best-effort)
        if self.redis:
            asyncio.create_task(self._redis_publish(msg))

        # Deliver to matching subscribers
        matching = [
            sub
            for sub in self._subscriptions.values()
            if sub.matches(topic) and (sub.filter_fn is None or sub.filter_fn(msg))
        ]

        if not matching:
            self._stats.dropped += 1
            return msg.msg_id

        # Sort by priority for dispatch order
        for sub in matching:
            asyncio.create_task(self._deliver(msg, sub))

        return msg.msg_id

    async def _deliver(self, msg: Message, sub: Subscription):
        if msg.expired:
            self._stats.dropped += 1
            return

        msg.attempts += 1
        async with sub._semaphore:
            try:
                if asyncio.iscoroutinefunction(sub.handler):
                    await sub.handler(msg)
                else:
                    sub.handler(msg)
                self._stats.delivered += 1
            except Exception as e:
                self._stats.handler_errors += 1
                log.warning(
                    f"Handler error in sub {sub.sub_id} for topic '{msg.topic}': {e}"
                )
                if self._dlq_enabled:
                    await self._send_dlq(msg, str(e))

    async def _send_dlq(self, msg: Message, error: str):
        self._dlq.append(msg)
        self._stats.dlq_sent += 1
        try:
            entry = {**msg.to_dict(), "dlq_error": error, "dlq_ts": time.time()}
            with open(DLQ_FILE, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    async def _redis_publish(self, msg: Message):
        if not self.redis:
            return
        try:
            await self.redis.xadd(
                REDIS_STREAM,
                {
                    "msg_id": msg.msg_id,
                    "topic": msg.topic,
                    "payload": json.dumps(msg.payload),
                    "priority": msg.priority.value,
                    "source": msg.source,
                },
                maxlen=10000,
            )
        except Exception as e:
            log.warning(f"Redis publish error: {e}")

    def replay(
        self,
        topic_pattern: str | None = None,
        since_ts: float = 0.0,
        limit: int = 100,
    ) -> list[dict]:
        results = []
        for msg in reversed(self._history):
            if since_ts and msg.created_at < since_ts:
                break
            if topic_pattern:
                from fnmatch import fnmatch

                if not fnmatch(msg.topic, topic_pattern.replace(".*", ".*")):
                    continue
            results.append(msg.to_dict())
            if len(results) >= limit:
                break
        return list(reversed(results))

    def dlq_messages(self) -> list[dict]:
        return [m.to_dict() for m in self._dlq]

    def stats(self) -> dict:
        return {
            "published": self._stats.published,
            "delivered": self._stats.delivered,
            "dropped": self._stats.dropped,
            "dlq_sent": self._stats.dlq_sent,
            "handler_errors": self._stats.handler_errors,
            "subscriptions": self._stats.subscriptions,
            "history_size": len(self._history),
        }


def build_jarvis_message_bus() -> MessageBus:
    bus = MessageBus(dlq_enabled=True, max_history=1000)

    def log_all(msg: Message):
        log.debug(f"[{msg.topic}] {str(msg.payload)[:80]}")

    bus.subscribe("*", log_all)
    return bus


async def main():
    import sys

    bus = build_jarvis_message_bus()
    await bus.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        received: list[str] = []

        async def on_cluster(msg: Message):
            received.append(f"cluster: {msg.payload}")

        async def on_alert(msg: Message):
            received.append(f"alert: {msg.payload}")

        bus.subscribe("cluster.*", on_cluster)
        bus.subscribe("alerts.critical", on_alert)

        print("Publishing messages...")
        ids = []
        ids.append(
            await bus.publish(
                "cluster.node.m1", {"status": "healthy"}, source="monitor"
            )
        )
        ids.append(
            await bus.publish("cluster.gpu.thermal", {"temp": 72}, MessagePriority.HIGH)
        )
        ids.append(
            await bus.publish(
                "alerts.critical", "GPU overheating!", MessagePriority.CRITICAL
            )
        )
        ids.append(
            await bus.publish("trading.signal", {"pair": "BTC/USDT", "side": "long"})
        )

        await asyncio.sleep(0.1)  # let async handlers run

        print(f"\nPublished {len(ids)} messages")
        print(f"Received by handlers: {received}")

        print("\nHistory replay (cluster.*):")
        for entry in bus.replay("cluster.*"):
            print(f"  [{entry['topic']}] {entry['payload']}")

        print(f"\nStats: {json.dumps(bus.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(bus.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
