#!/usr/bin/env python3
"""
jarvis_outbox_relay — Transactional outbox pattern for reliable event publishing
Persists events to outbox before publishing, ensures at-least-once delivery
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.outbox_relay")

REDIS_PREFIX = "jarvis:outbox:"
OUTBOX_STREAM = "jarvis:outbox:stream"


class OutboxStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    DELIVERED = "delivered"
    FAILED = "failed"
    DEAD = "dead"  # max retries exceeded


@dataclass
class OutboxEntry:
    entry_id: str
    topic: str
    payload: dict[str, Any]
    status: OutboxStatus = OutboxStatus.PENDING
    created_at: float = field(default_factory=time.time)
    attempts: int = 0
    last_attempt: float = 0.0
    next_retry: float = 0.0
    error: str = ""
    max_retries: int = 5
    source: str = ""

    def to_dict(self) -> dict:
        return {
            "entry_id": self.entry_id,
            "topic": self.topic,
            "payload": self.payload,
            "status": self.status.value,
            "created_at": self.created_at,
            "attempts": self.attempts,
            "last_attempt": self.last_attempt,
            "error": self.error,
            "source": self.source,
        }


class OutboxRelay:
    def __init__(self, batch_size: int = 50, poll_interval_s: float = 1.0):
        self.redis: aioredis.Redis | None = None
        self._entries: dict[str, OutboxEntry] = {}
        self._publishers: dict[str, Callable] = {}  # topic → publish fn
        self._default_publisher: Callable | None = None
        self._batch_size = batch_size
        self._poll_interval = poll_interval_s
        self._relay_task: asyncio.Task | None = None
        self._running = False
        self._stats: dict[str, int] = {
            "enqueued": 0,
            "delivered": 0,
            "failed": 0,
            "dead": 0,
            "retried": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
            # Load pending entries from Redis on startup
            await self._load_pending()
        except Exception:
            self.redis = None

    async def _load_pending(self):
        if not self.redis:
            return
        try:
            keys = await self.redis.keys(f"{REDIS_PREFIX}entry:*")
            for key in keys[:1000]:
                raw = await self.redis.get(key)
                if raw:
                    data = json.loads(raw)
                    if data.get("status") in ("pending", "processing", "failed"):
                        entry = OutboxEntry(
                            entry_id=data["entry_id"],
                            topic=data["topic"],
                            payload=data["payload"],
                            status=OutboxStatus.PENDING,
                            created_at=data.get("created_at", time.time()),
                            attempts=data.get("attempts", 0),
                            max_retries=data.get("max_retries", 5),
                            source=data.get("source", ""),
                        )
                        self._entries[entry.entry_id] = entry
            log.info(f"Loaded {len(self._entries)} pending outbox entries from Redis")
        except Exception as e:
            log.warning(f"Could not load outbox from Redis: {e}")

    def register_publisher(self, topic: str, fn: Callable):
        self._publishers[topic] = fn

    def set_default_publisher(self, fn: Callable):
        self._default_publisher = fn

    async def enqueue(
        self,
        topic: str,
        payload: dict,
        source: str = "",
        max_retries: int = 5,
    ) -> str:
        import secrets

        entry_id = secrets.token_hex(8)
        entry = OutboxEntry(
            entry_id=entry_id,
            topic=topic,
            payload=payload,
            source=source,
            max_retries=max_retries,
        )
        self._entries[entry_id] = entry
        self._stats["enqueued"] += 1

        if self.redis:
            asyncio.create_task(self._redis_save(entry))

        log.debug(f"Enqueued {topic!r} [{entry_id}]")
        return entry_id

    async def _redis_save(self, entry: OutboxEntry):
        if not self.redis:
            return
        try:
            await self.redis.setex(
                f"{REDIS_PREFIX}entry:{entry.entry_id}",
                86400,
                json.dumps(entry.to_dict()),
            )
        except Exception:
            pass

    async def _publish_entry(self, entry: OutboxEntry) -> bool:
        publisher = self._publishers.get(entry.topic) or self._default_publisher
        if not publisher:
            # No publisher — push to Redis stream as fallback
            if self.redis:
                try:
                    await self.redis.xadd(
                        OUTBOX_STREAM,
                        {"topic": entry.topic, "data": json.dumps(entry.payload)},
                        maxlen=50000,
                    )
                    return True
                except Exception as e:
                    entry.error = str(e)
                    return False
            return False

        try:
            coro = publisher(entry.topic, entry.payload)
            if asyncio.iscoroutine(coro):
                await asyncio.wait_for(coro, timeout=10.0)
            return True
        except Exception as e:
            entry.error = str(e)
            return False

    async def _relay_loop(self):
        while self._running:
            await self._flush_batch()
            await asyncio.sleep(self._poll_interval)

    async def _flush_batch(self):
        now = time.time()
        pending = [
            e
            for e in self._entries.values()
            if e.status in (OutboxStatus.PENDING, OutboxStatus.FAILED)
            and e.next_retry <= now
        ]
        pending.sort(key=lambda e: e.created_at)
        batch = pending[: self._batch_size]

        for entry in batch:
            entry.status = OutboxStatus.PROCESSING
            entry.attempts += 1
            entry.last_attempt = now

            ok = await self._publish_entry(entry)

            if ok:
                entry.status = OutboxStatus.DELIVERED
                self._stats["delivered"] += 1
                if self.redis:
                    asyncio.create_task(self._redis_save(entry))
                log.debug(
                    f"Delivered outbox entry {entry.entry_id!r} topic={entry.topic!r}"
                )
            else:
                self._stats["failed"] += 1
                if entry.attempts >= entry.max_retries:
                    entry.status = OutboxStatus.DEAD
                    self._stats["dead"] += 1
                    log.error(
                        f"Outbox entry {entry.entry_id!r} dead after {entry.attempts} attempts"
                    )
                else:
                    entry.status = OutboxStatus.FAILED
                    backoff = min(300, 2**entry.attempts)
                    entry.next_retry = now + backoff
                    self._stats["retried"] += 1
                    log.warning(f"Outbox entry {entry.entry_id!r} retry in {backoff}s")

                if self.redis:
                    asyncio.create_task(self._redis_save(entry))

    def start(self):
        self._running = True
        self._relay_task = asyncio.create_task(self._relay_loop())
        log.info("OutboxRelay started")

    async def stop(self):
        self._running = False
        if self._relay_task:
            self._relay_task.cancel()
            try:
                await self._relay_task
            except asyncio.CancelledError:
                pass

    async def flush(self):
        """Manual flush — deliver all pending entries now."""
        await self._flush_batch()

    def pending_count(self) -> int:
        return sum(
            1
            for e in self._entries.values()
            if e.status in (OutboxStatus.PENDING, OutboxStatus.FAILED)
        )

    def dead_entries(self, limit: int = 20) -> list[dict]:
        dead = [e for e in self._entries.values() if e.status == OutboxStatus.DEAD]
        return [e.to_dict() for e in dead[-limit:]]

    def stats(self) -> dict:
        by_status: dict[str, int] = {}
        for e in self._entries.values():
            by_status[e.status.value] = by_status.get(e.status.value, 0) + 1
        return {
            **self._stats,
            "total_entries": len(self._entries),
            "pending": self.pending_count(),
            "by_status": by_status,
        }


def build_jarvis_outbox_relay() -> OutboxRelay:
    relay = OutboxRelay(batch_size=100, poll_interval_s=0.5)

    # Default publisher: log and push to Redis stream (real publisher wired at runtime)
    async def default_pub(topic: str, payload: dict):
        log.debug(f"[outbox] publish topic={topic!r} payload_keys={list(payload)}")

    relay.set_default_publisher(default_pub)
    return relay


async def main():
    import sys

    relay = build_jarvis_outbox_relay()
    await relay.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Outbox relay demo...")

        # Enqueue events
        ids = []
        for i in range(5):
            eid = await relay.enqueue(
                "inference.result",
                {"request_id": f"req-{i}", "model": "qwen3.5", "tokens": 100 + i * 10},
                source="inference-gw",
            )
            ids.append(eid)

        await relay.enqueue(
            "alert.fired", {"severity": "warning", "msg": "GPU temp high"}
        )
        await relay.enqueue("budget.updated", {"agent": "trading", "delta": -0.05})

        print(f"  Enqueued: {relay.pending_count()} pending")

        # Flush
        await relay.flush()

        print(f"  After flush: pending={relay.pending_count()}")
        print(f"\nStats: {json.dumps(relay.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(relay.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
