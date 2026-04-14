#!/usr/bin/env python3
"""
jarvis_inbox_processor — Inbox message processor with routing and deduplication
Consume from Redis Streams, route to handlers, track processing state
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.inbox_processor")

REDIS_PREFIX = "jarvis:inbox:"
CONSUMER_GROUP = "jarvis-inbox"


class InboxMessageState(str, Enum):
    RECEIVED = "received"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class InboxMessage:
    message_id: str
    stream: str
    topic: str
    payload: dict[str, Any]
    state: InboxMessageState = InboxMessageState.RECEIVED
    received_at: float = field(default_factory=time.time)
    processed_at: float = 0.0
    attempts: int = 0
    error: str = ""
    result: Any = None

    def to_dict(self) -> dict:
        return {
            "message_id": self.message_id,
            "stream": self.stream,
            "topic": self.topic,
            "payload": self.payload,
            "state": self.state.value,
            "received_at": self.received_at,
            "processed_at": self.processed_at,
            "attempts": self.attempts,
            "error": self.error,
        }


@dataclass
class RouteRule:
    pattern: str  # exact topic or prefix with *
    handler: Callable
    max_retries: int = 3
    timeout_s: float = 30.0
    concurrency: int = 1


class InboxProcessor:
    def __init__(
        self,
        consumer_name: str = "processor-1",
        poll_interval_s: float = 0.5,
        batch_size: int = 10,
    ):
        self.redis: aioredis.Redis | None = None
        self._consumer_name = consumer_name
        self._poll_interval = poll_interval_s
        self._batch_size = batch_size
        self._routes: list[RouteRule] = []
        self._fallback: Callable | None = None
        self._seen: set[str] = set()  # dedup cache
        self._max_seen = 50_000
        self._history: list[InboxMessage] = []
        self._max_history = 1000
        self._streams: list[str] = []
        self._running = False
        self._task: asyncio.Task | None = None
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._stats: dict[str, int] = {
            "received": 0,
            "processed": 0,
            "failed": 0,
            "skipped": 0,
            "deduplicated": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def subscribe(self, stream: str):
        if stream not in self._streams:
            self._streams.append(stream)

    def route(
        self,
        pattern: str,
        handler: Callable,
        max_retries: int = 3,
        timeout_s: float = 30.0,
        concurrency: int = 1,
    ):
        rule = RouteRule(pattern, handler, max_retries, timeout_s, concurrency)
        self._routes.append(rule)
        if concurrency > 1:
            self._semaphores[pattern] = asyncio.Semaphore(concurrency)

    def set_fallback(self, fn: Callable):
        self._fallback = fn

    def _match_route(self, topic: str) -> RouteRule | None:
        for rule in self._routes:
            p = rule.pattern
            if p.endswith("*"):
                if topic.startswith(p[:-1]):
                    return rule
            elif p == topic or p == "*":
                return rule
        return None

    def _is_duplicate(self, message_id: str) -> bool:
        if message_id in self._seen:
            return True
        self._seen.add(message_id)
        if len(self._seen) > self._max_seen:
            # Evict half
            to_remove = list(self._seen)[: self._max_seen // 2]
            for k in to_remove:
                self._seen.discard(k)
        return False

    async def _process_message(self, msg: InboxMessage):
        if self._is_duplicate(msg.message_id):
            msg.state = InboxMessageState.SKIPPED
            self._stats["deduplicated"] += 1
            return

        self._stats["received"] += 1
        msg.state = InboxMessageState.PROCESSING

        rule = self._match_route(msg.topic)
        if rule is None:
            if self._fallback:
                rule = RouteRule("*", self._fallback)
            else:
                msg.state = InboxMessageState.SKIPPED
                self._stats["skipped"] += 1
                log.debug(f"No route for topic {msg.topic!r} — skipped")
                return

        sem = self._semaphores.get(rule.pattern)

        async def _invoke():
            for attempt in range(1, rule.max_retries + 2):
                msg.attempts = attempt
                try:
                    coro = rule.handler(msg)
                    if asyncio.iscoroutine(coro):
                        result = await asyncio.wait_for(coro, timeout=rule.timeout_s)
                    else:
                        result = coro
                    msg.result = result
                    msg.state = InboxMessageState.PROCESSED
                    msg.processed_at = time.time()
                    self._stats["processed"] += 1
                    return
                except Exception as e:
                    msg.error = str(e)
                    if attempt <= rule.max_retries:
                        await asyncio.sleep(0.1 * attempt)
                    else:
                        msg.state = InboxMessageState.FAILED
                        self._stats["failed"] += 1
                        log.error(
                            f"Message {msg.message_id} failed after {attempt} attempts: {e}"
                        )

        if sem:
            async with sem:
                await _invoke()
        else:
            await _invoke()

        self._history.append(msg)
        if len(self._history) > self._max_history:
            self._history.pop(0)

    async def _poll_streams(self):
        if not self.redis or not self._streams:
            return

        # Ensure consumer groups exist
        for stream in self._streams:
            try:
                await self.redis.xgroup_create(
                    stream, CONSUMER_GROUP, id="0", mkstream=True
                )
            except Exception:
                pass  # group already exists

        while self._running:
            try:
                stream_ids = {s: ">" for s in self._streams}
                results = await self.redis.xreadgroup(
                    CONSUMER_GROUP,
                    self._consumer_name,
                    stream_ids,
                    count=self._batch_size,
                    block=int(self._poll_interval * 1000),
                )
                if results:
                    tasks = []
                    for stream_name, messages in results:
                        for msg_id, data in messages:
                            raw = data.get("data", "{}")
                            try:
                                payload = json.loads(raw)
                            except Exception:
                                payload = dict(data)
                            topic = payload.get("topic", payload.get("type", "unknown"))
                            msg = InboxMessage(
                                message_id=msg_id,
                                stream=stream_name,
                                topic=topic,
                                payload=payload,
                            )
                            tasks.append(self._process_message(msg))
                            # ACK after processing
                            await self.redis.xack(stream_name, CONSUMER_GROUP, msg_id)

                    if tasks:
                        await asyncio.gather(*tasks)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"InboxProcessor poll error: {e}")
                await asyncio.sleep(self._poll_interval)

    async def _poll_local(self):
        """Fallback polling loop when Redis unavailable — no-op."""
        while self._running:
            await asyncio.sleep(self._poll_interval)

    def start(self):
        self._running = True
        if self.redis and self._streams:
            self._task = asyncio.create_task(self._poll_streams())
        else:
            self._task = asyncio.create_task(self._poll_local())
        log.info(f"InboxProcessor started, streams={self._streams}")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def inject(
        self, topic: str, payload: dict, message_id: str | None = None
    ) -> InboxMessage:
        """Inject a message directly (testing / local dispatch)."""
        import secrets

        msg = InboxMessage(
            message_id=message_id or secrets.token_hex(8),
            stream="local",
            topic=topic,
            payload=payload,
        )
        await self._process_message(msg)
        return msg

    def recent(self, limit: int = 20) -> list[dict]:
        return [m.to_dict() for m in self._history[-limit:]]

    def stats(self) -> dict:
        return {
            **self._stats,
            "routes": len(self._routes),
            "streams": len(self._streams),
            "history_size": len(self._history),
            "dedup_cache_size": len(self._seen),
        }


def build_jarvis_inbox_processor() -> InboxProcessor:
    proc = InboxProcessor(consumer_name="jarvis-main", batch_size=20)

    # Subscribe to standard JARVIS streams
    proc.subscribe("jarvis:outbox:stream")
    proc.subscribe("jarvis:events:stream")

    # Route handlers
    async def handle_inference(msg: InboxMessage) -> dict:
        log.debug(f"Handling inference message: {msg.payload.get('model')}")
        return {"handled": True}

    async def handle_alert(msg: InboxMessage) -> dict:
        log.warning(f"Alert: {msg.payload.get('message', msg.payload)}")
        return {"alerted": True}

    async def handle_fallback(msg: InboxMessage):
        log.debug(f"Fallback handler for topic={msg.topic!r}")

    proc.route("inference.*", handle_inference, concurrency=4)
    proc.route("alert.*", handle_alert)
    proc.set_fallback(handle_fallback)

    return proc


async def main():
    import sys

    proc = build_jarvis_inbox_processor()
    await proc.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Inbox processor demo...")

        # Inject messages directly
        msgs = [
            ("inference.run", {"model": "qwen3.5", "prompt": "hello"}),
            ("inference.run", {"model": "deepseek-r1", "prompt": "reason"}),
            ("alert.gpu", {"severity": "warning", "message": "GPU temp 82C"}),
            ("trading.signal", {"symbol": "BTC", "action": "buy"}),
            ("unknown.topic", {"data": 42}),
        ]

        for topic, payload in msgs:
            m = await proc.inject(topic, payload)
            icon = {"processed": "✅", "failed": "❌", "skipped": "⏭"}.get(
                m.state.value, "?"
            )
            print(f"  {icon} {topic:<25} → {m.state.value}")

        # Dedup test
        m2 = await proc.inject(
            "inference.run", {"model": "qwen3.5"}, message_id="fixed-id"
        )
        m3 = await proc.inject(
            "inference.run", {"model": "qwen3.5"}, message_id="fixed-id"
        )
        print(f"\n  Dedup test: first={m2.state.value} second={m3.state.value}")

        print(f"\nStats: {json.dumps(proc.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(proc.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
