#!/usr/bin/env python3
"""
jarvis_dead_letter_queue — Dead letter queue for failed messages and tasks
Capture, inspect, replay, and purge undeliverable messages with TTL management
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.dead_letter_queue")

REDIS_PREFIX = "jarvis:dlq:"
DLQ_STREAM = "jarvis:dlq:stream"


class DLQReason(str, Enum):
    MAX_RETRIES = "max_retries"
    TIMEOUT = "timeout"
    VALIDATION_ERROR = "validation_error"
    DESERIALIZATION_ERROR = "deserialization_error"
    HANDLER_MISSING = "handler_missing"
    REJECTED = "rejected"
    POISON_PILL = "poison_pill"


class DLQEntryState(str, Enum):
    DEAD = "dead"
    REPLAYING = "replaying"
    REPLAYED = "replayed"
    ARCHIVED = "archived"


@dataclass
class DLQEntry:
    entry_id: str
    source_queue: str
    topic: str
    payload: dict[str, Any]
    reason: DLQReason
    error: str = ""
    original_attempts: int = 0
    created_at: float = field(default_factory=time.time)
    state: DLQEntryState = DLQEntryState.DEAD
    replay_count: int = 0
    last_replayed: float = 0.0
    ttl_s: float = 86400.0 * 7  # 7 days default
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def age_s(self) -> float:
        return time.time() - self.created_at

    @property
    def is_expired(self) -> bool:
        return self.age_s > self.ttl_s

    def to_dict(self) -> dict:
        return {
            "entry_id": self.entry_id,
            "source_queue": self.source_queue,
            "topic": self.topic,
            "payload": self.payload,
            "reason": self.reason.value,
            "error": self.error,
            "original_attempts": self.original_attempts,
            "created_at": self.created_at,
            "age_s": round(self.age_s, 1),
            "state": self.state.value,
            "replay_count": self.replay_count,
            "tags": self.tags,
        }


class DeadLetterQueue:
    def __init__(self, max_entries: int = 10_000):
        self.redis: aioredis.Redis | None = None
        self._entries: dict[str, DLQEntry] = {}
        self._max_entries = max_entries
        self._replay_handlers: dict[str, Callable] = {}
        self._default_replay: Callable | None = None
        self._stats: dict[str, int] = {
            "received": 0,
            "replayed": 0,
            "replay_success": 0,
            "replay_failed": 0,
            "purged": 0,
            "archived": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def register_replay_handler(self, topic: str, fn: Callable):
        self._replay_handlers[topic] = fn

    def set_default_replay_handler(self, fn: Callable):
        self._default_replay = fn

    async def push(
        self,
        source_queue: str,
        topic: str,
        payload: dict,
        reason: DLQReason,
        error: str = "",
        original_attempts: int = 0,
        tags: list[str] | None = None,
        ttl_s: float = 86400.0 * 7,
    ) -> DLQEntry:
        import secrets

        # Evict oldest if at capacity
        if len(self._entries) >= self._max_entries:
            oldest_id = min(self._entries, key=lambda k: self._entries[k].created_at)
            del self._entries[oldest_id]

        entry_id = secrets.token_hex(10)
        entry = DLQEntry(
            entry_id=entry_id,
            source_queue=source_queue,
            topic=topic,
            payload=payload,
            reason=reason,
            error=error,
            original_attempts=original_attempts,
            tags=tags or [],
            ttl_s=ttl_s,
        )
        self._entries[entry_id] = entry
        self._stats["received"] += 1

        log.warning(
            f"DLQ [{entry_id}] source={source_queue!r} topic={topic!r} "
            f"reason={reason.value} attempts={original_attempts}"
        )

        if self.redis:
            asyncio.create_task(self._redis_push(entry))

        return entry

    async def _redis_push(self, entry: DLQEntry):
        if not self.redis:
            return
        try:
            await self.redis.xadd(
                DLQ_STREAM,
                {"data": json.dumps(entry.to_dict())},
                maxlen=self._max_entries,
            )
            await self.redis.setex(
                f"{REDIS_PREFIX}{entry.entry_id}",
                int(entry.ttl_s),
                json.dumps(entry.to_dict()),
            )
        except Exception:
            pass

    async def replay(self, entry_id: str) -> bool:
        entry = self._entries.get(entry_id)
        if not entry:
            return False
        if entry.state == DLQEntryState.REPLAYED:
            log.debug(f"Entry {entry_id} already replayed")
            return True

        entry.state = DLQEntryState.REPLAYING
        self._stats["replayed"] += 1

        handler = self._replay_handlers.get(entry.topic) or self._default_replay
        if not handler:
            log.error(f"No replay handler for topic {entry.topic!r}")
            entry.state = DLQEntryState.DEAD
            self._stats["replay_failed"] += 1
            return False

        try:
            coro = handler(entry.source_queue, entry.topic, entry.payload)
            if asyncio.iscoroutine(coro):
                await asyncio.wait_for(coro, timeout=30.0)
            entry.state = DLQEntryState.REPLAYED
            entry.replay_count += 1
            entry.last_replayed = time.time()
            self._stats["replay_success"] += 1
            log.info(f"Replayed DLQ entry {entry_id} topic={entry.topic!r}")
            return True
        except Exception as e:
            entry.state = DLQEntryState.DEAD
            entry.error = f"replay failed: {e}"
            self._stats["replay_failed"] += 1
            log.error(f"Replay failed for {entry_id}: {e}")
            return False

    async def replay_all(
        self,
        topic: str | None = None,
        reason: DLQReason | None = None,
        limit: int = 100,
    ) -> dict[str, int]:
        candidates = [
            e
            for e in self._entries.values()
            if e.state == DLQEntryState.DEAD
            and (topic is None or e.topic == topic)
            and (reason is None or e.reason == reason)
        ]
        candidates = candidates[:limit]

        ok = 0
        fail = 0
        for entry in candidates:
            if await self.replay(entry.entry_id):
                ok += 1
            else:
                fail += 1

        return {"replayed": ok, "failed": fail, "total": len(candidates)}

    def purge_expired(self) -> int:
        expired = [eid for eid, e in self._entries.items() if e.is_expired]
        for eid in expired:
            del self._entries[eid]
        self._stats["purged"] += len(expired)
        if expired:
            log.info(f"Purged {len(expired)} expired DLQ entries")
        return len(expired)

    def archive(self, entry_id: str) -> bool:
        entry = self._entries.get(entry_id)
        if not entry:
            return False
        entry.state = DLQEntryState.ARCHIVED
        self._stats["archived"] += 1
        return True

    def list(
        self,
        topic: str | None = None,
        reason: DLQReason | None = None,
        state: DLQEntryState | None = None,
        limit: int = 50,
    ) -> list[dict]:
        entries = list(self._entries.values())
        if topic:
            entries = [e for e in entries if e.topic == topic]
        if reason:
            entries = [e for e in entries if e.reason == reason]
        if state:
            entries = [e for e in entries if e.state == state]
        entries.sort(key=lambda e: -e.created_at)
        return [e.to_dict() for e in entries[:limit]]

    def get(self, entry_id: str) -> DLQEntry | None:
        return self._entries.get(entry_id)

    def top_failing_topics(self, limit: int = 10) -> list[dict]:
        counts: dict[str, int] = {}
        for e in self._entries.values():
            counts[e.topic] = counts.get(e.topic, 0) + 1
        return [
            {"topic": t, "count": c}
            for t, c in sorted(counts.items(), key=lambda x: -x[1])[:limit]
        ]

    def stats(self) -> dict:
        by_reason: dict[str, int] = {}
        by_state: dict[str, int] = {}
        for e in self._entries.values():
            by_reason[e.reason.value] = by_reason.get(e.reason.value, 0) + 1
            by_state[e.state.value] = by_state.get(e.state.value, 0) + 1
        return {
            **self._stats,
            "total": len(self._entries),
            "by_reason": by_reason,
            "by_state": by_state,
        }


def build_jarvis_dead_letter_queue() -> DeadLetterQueue:
    dlq = DeadLetterQueue(max_entries=10_000)

    async def default_replay(source: str, topic: str, payload: dict):
        log.info(
            f"[DLQ replay] source={source} topic={topic} payload_keys={list(payload)}"
        )

    dlq.set_default_replay_handler(default_replay)
    return dlq


async def main():
    import sys

    dlq = build_jarvis_dead_letter_queue()
    await dlq.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Dead letter queue demo...")

        # Simulate failed messages
        entries = []
        for i in range(3):
            e = await dlq.push(
                source_queue="inference.requests",
                topic="inference.run",
                payload={"model": "qwen3.5", "prompt": f"test {i}"},
                reason=DLQReason.MAX_RETRIES,
                error="handler timeout after 30s",
                original_attempts=5,
            )
            entries.append(e)

        await dlq.push(
            source_queue="trading.signals",
            topic="trade.execute",
            payload={"symbol": "BTC", "side": "buy", "amount": 0.01},
            reason=DLQReason.VALIDATION_ERROR,
            error="insufficient balance",
            original_attempts=1,
        )

        await dlq.push(
            source_queue="alert.pipeline",
            topic="alert.notify",
            payload={"severity": "critical", "msg": "GPU down"},
            reason=DLQReason.HANDLER_MISSING,
            original_attempts=0,
        )

        print(f"\n  Total DLQ entries: {len(dlq._entries)}")
        print(f"  Top failing topics: {dlq.top_failing_topics()}")

        # Replay one
        result = await dlq.replay(entries[0].entry_id)
        print(f"\n  Replay entry[0] → {'OK' if result else 'FAIL'}")

        # Replay all inference
        rep = await dlq.replay_all(topic="inference.run")
        print(f"  Replay all inference.run → {rep}")

        print(f"\nStats: {json.dumps(dlq.stats(), indent=2)}")

    elif cmd == "list":
        for e in dlq.list():
            print(
                f"  {e['entry_id']} {e['topic']:<25} {e['reason']:<20} attempts={e['original_attempts']}"
            )

    elif cmd == "stats":
        print(json.dumps(dlq.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
