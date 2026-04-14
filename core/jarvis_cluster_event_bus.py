#!/usr/bin/env python3
"""
jarvis_cluster_event_bus — Distributed event bus for inter-agent communication
Pub/sub with topic routing, persistent replay, and delivery guarantees via Redis
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.cluster_event_bus")

REDIS_PREFIX = "jarvis:eventbus:"
REDIS_STREAM_PREFIX = "jarvis:stream:"
MAX_STREAM_LEN = 1000


class EventPriority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class BusEvent:
    event_id: str
    topic: str
    payload: dict
    source: str = ""
    priority: EventPriority = EventPriority.NORMAL
    ttl_s: float = 300.0
    ts: float = field(default_factory=time.time)
    headers: dict = field(default_factory=dict)

    @property
    def expired(self) -> bool:
        return self.ttl_s > 0 and (time.time() - self.ts) > self.ttl_s

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "topic": self.topic,
            "payload": self.payload,
            "source": self.source,
            "priority": self.priority.value,
            "ts": self.ts,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BusEvent":
        return cls(
            event_id=d["event_id"],
            topic=d["topic"],
            payload=d.get("payload", {}),
            source=d.get("source", ""),
            priority=EventPriority(d.get("priority", "normal")),
            ttl_s=d.get("ttl_s", 300.0),
            ts=d.get("ts", time.time()),
        )


@dataclass
class Subscription:
    sub_id: str
    topic_pattern: str  # exact topic or wildcard like "cluster.*"
    handler: Callable
    source_filter: str = ""  # only accept from this source
    priority_min: EventPriority = EventPriority.LOW
    created_at: float = field(default_factory=time.time)
    received: int = 0
    errors: int = 0

    def matches(self, event: BusEvent) -> bool:
        # Topic matching: exact or wildcard
        if self.topic_pattern.endswith(".*"):
            prefix = self.topic_pattern[:-2]
            if not event.topic.startswith(prefix):
                return False
        elif self.topic_pattern != "*" and self.topic_pattern != event.topic:
            return False
        if self.source_filter and event.source != self.source_filter:
            return False
        _priority_order = [
            EventPriority.LOW,
            EventPriority.NORMAL,
            EventPriority.HIGH,
            EventPriority.CRITICAL,
        ]
        if _priority_order.index(event.priority) < _priority_order.index(
            self.priority_min
        ):
            return False
        return True


class ClusterEventBus:
    def __init__(self, node_id: str = "local"):
        self.redis: aioredis.Redis | None = None
        self._node_id = node_id
        self._subscriptions: dict[str, Subscription] = {}
        self._local_queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._history: list[BusEvent] = []
        self._max_history = 500
        self._running = False
        self._stats: dict[str, int] = {
            "published": 0,
            "delivered": 0,
            "dropped": 0,
            "errors": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
            log.info(f"EventBus {self._node_id}: Redis connected")
        except Exception:
            self.redis = None
            log.warning(f"EventBus {self._node_id}: Redis unavailable, local-only mode")

    def subscribe(
        self,
        topic_pattern: str,
        handler: Callable,
        source_filter: str = "",
        priority_min: EventPriority = EventPriority.LOW,
    ) -> str:
        sub_id = str(uuid.uuid4())[:10]
        sub = Subscription(
            sub_id=sub_id,
            topic_pattern=topic_pattern,
            handler=handler,
            source_filter=source_filter,
            priority_min=priority_min,
        )
        self._subscriptions[sub_id] = sub
        log.debug(f"Subscribed {sub_id} to '{topic_pattern}'")
        return sub_id

    def unsubscribe(self, sub_id: str):
        self._subscriptions.pop(sub_id, None)

    def publish(
        self,
        topic: str,
        payload: dict,
        source: str = "",
        priority: EventPriority = EventPriority.NORMAL,
        ttl_s: float = 300.0,
    ) -> BusEvent:
        event = BusEvent(
            event_id=str(uuid.uuid4())[:12],
            topic=topic,
            payload=payload,
            source=source or self._node_id,
            priority=priority,
            ttl_s=ttl_s,
        )
        self._stats["published"] += 1

        # Store in history
        self._history.append(event)
        if len(self._history) > self._max_history:
            self._history.pop(0)

        # Deliver locally
        self._deliver_local(event)

        # Publish to Redis for cross-node delivery
        if self.redis:
            asyncio.create_task(self._publish_redis(event))

        return event

    def _deliver_local(self, event: BusEvent):
        if event.expired:
            self._stats["dropped"] += 1
            return

        for sub in list(self._subscriptions.values()):
            if sub.matches(event):
                sub.received += 1
                self._stats["delivered"] += 1
                try:
                    if asyncio.iscoroutinefunction(sub.handler):
                        asyncio.create_task(sub.handler(event))
                    else:
                        sub.handler(event)
                except Exception as e:
                    sub.errors += 1
                    self._stats["errors"] += 1
                    log.warning(f"Handler error for sub {sub.sub_id}: {e}")

    async def _publish_redis(self, event: BusEvent):
        if not self.redis:
            return
        try:
            stream_key = f"{REDIS_STREAM_PREFIX}{event.topic}"
            await self.redis.xadd(
                stream_key,
                {"data": json.dumps(event.to_dict())},
                maxlen=MAX_STREAM_LEN,
            )
            # Also pub/sub for low-latency delivery
            channel = f"{REDIS_PREFIX}topic:{event.topic}"
            await self.redis.publish(channel, json.dumps(event.to_dict()))
        except Exception as e:
            log.warning(f"Redis publish error: {e}")

    async def start_redis_listener(self, topics: list[str]):
        """Listen for events from other nodes via Redis pub/sub."""
        if not self.redis:
            return
        self._running = True
        pubsub = self.redis.pubsub()
        channels = [f"{REDIS_PREFIX}topic:{t}" for t in topics]
        channels.append(f"{REDIS_PREFIX}topic:*")  # wildcard if supported
        await pubsub.subscribe(*channels)

        async def listen():
            async for msg in pubsub.listen():
                if not self._running:
                    break
                if msg["type"] == "message":
                    try:
                        d = json.loads(msg["data"])
                        event = BusEvent.from_dict(d)
                        if (
                            event.source != self._node_id
                        ):  # avoid re-delivering own events
                            self._deliver_local(event)
                    except Exception:
                        pass

        asyncio.create_task(listen())

    def stop(self):
        self._running = False

    def replay(
        self,
        topic: str | None = None,
        since_ts: float = 0.0,
        n: int = 50,
    ) -> list[BusEvent]:
        events = self._history
        if topic:
            events = [e for e in events if e.topic == topic or topic == "*"]
        if since_ts:
            events = [e for e in events if e.ts >= since_ts]
        return list(reversed(events))[:n]

    async def replay_from_stream(
        self, topic: str, since_id: str = "0"
    ) -> list[BusEvent]:
        if not self.redis:
            return []
        stream_key = f"{REDIS_STREAM_PREFIX}{topic}"
        try:
            entries = await self.redis.xrange(stream_key, min=since_id, count=100)
            events = []
            for _, fields in entries:
                d = json.loads(fields.get("data", "{}"))
                events.append(BusEvent.from_dict(d))
            return events
        except Exception:
            return []

    def subscription_stats(self) -> list[dict]:
        return [
            {
                "sub_id": s.sub_id,
                "topic_pattern": s.topic_pattern,
                "received": s.received,
                "errors": s.errors,
            }
            for s in self._subscriptions.values()
        ]

    def stats(self) -> dict:
        return {
            **self._stats,
            "subscriptions": len(self._subscriptions),
            "history": len(self._history),
            "node_id": self._node_id,
        }


def build_jarvis_event_bus(node_id: str = "jarvis-core") -> ClusterEventBus:
    bus = ClusterEventBus(node_id=node_id)

    # Wire up standard log handler
    def log_high_priority(event: BusEvent):
        if event.priority in (EventPriority.HIGH, EventPriority.CRITICAL):
            log.warning(
                f"[BUS/{event.priority.value}] {event.topic}: {str(event.payload)[:80]}"
            )

    bus.subscribe("*", log_high_priority, priority_min=EventPriority.HIGH)
    return bus


async def main():
    import sys

    bus = build_jarvis_event_bus("demo-node")
    await bus.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        received_events = []

        def on_cluster(event: BusEvent):
            received_events.append(event)

        async def on_alert(event: BusEvent):
            print(
                f"  [ALERT HANDLER] {event.topic}: {event.payload.get('message', '')[:60]}"
            )

        bus.subscribe("cluster.*", on_cluster)
        bus.subscribe("alert.*", on_alert, priority_min=EventPriority.HIGH)

        # Publish events
        events = [
            (
                "cluster.node_up",
                {"node": "m1", "status": "online"},
                EventPriority.NORMAL,
            ),
            ("cluster.gpu_warn", {"node": "m1", "temp": 82}, EventPriority.HIGH),
            (
                "alert.memory",
                {"message": "VRAM usage critical: 11.8/12GB"},
                EventPriority.CRITICAL,
            ),
            (
                "model.loaded",
                {"model": "qwen3.5-9b", "node": "m1"},
                EventPriority.NORMAL,
            ),
            (
                "cluster.node_down",
                {"node": "m3", "reason": "timeout"},
                EventPriority.HIGH,
            ),
        ]
        print("Publishing events:")
        for topic, payload, priority in events:
            e = bus.publish(topic, payload, source="demo", priority=priority)
            print(f"  [{priority.value:<10}] {topic:<30} id={e.event_id}")

        await asyncio.sleep(0.05)  # let async handlers run

        print(f"\nCluster events received by subscriber: {len(received_events)}")
        print("\nReplay (cluster.*):")
        for e in bus.replay(topic="cluster.*"):
            print(f"  {e.event_id} {e.topic:<30} {e.payload}")

        print("\nSubscription stats:")
        for s in bus.subscription_stats():
            print(f"  {s['topic_pattern']:<20} received={s['received']}")

        print(f"\nStats: {json.dumps(bus.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(bus.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
