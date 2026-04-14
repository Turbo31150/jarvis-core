#!/usr/bin/env python3
"""
jarvis_priority_queue — Async priority queue with aging, deadlines, and groups
Multi-level priority queue with starvation prevention and deadline enforcement
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.priority_queue")

REDIS_PREFIX = "jarvis:pq:"


class Priority(IntEnum):
    CRITICAL = 10
    HIGH = 8
    NORMAL = 5
    LOW = 3
    BACKGROUND = 1


@dataclass(order=True)
class QueueItem:
    # Comparison key: lower sort_key = higher priority
    sort_key: float = field(compare=True)
    # Non-compared fields
    item_id: str = field(compare=False)
    payload: Any = field(compare=False)
    priority: int = field(compare=False)
    group: str = field(compare=False, default="default")
    deadline: float = field(compare=False, default=0.0)  # 0 = no deadline
    enqueued_at: float = field(compare=False, default_factory=time.time)
    attempts: int = field(compare=False, default=0)
    tags: list[str] = field(compare=False, default_factory=list)

    @property
    def expired(self) -> bool:
        return self.deadline > 0 and time.time() > self.deadline

    @property
    def wait_ms(self) -> float:
        return (time.time() - self.enqueued_at) * 1000

    def to_dict(self) -> dict:
        return {
            "item_id": self.item_id,
            "priority": self.priority,
            "group": self.group,
            "deadline": self.deadline,
            "enqueued_at": self.enqueued_at,
            "wait_ms": round(self.wait_ms, 1),
            "attempts": self.attempts,
            "tags": self.tags,
        }


class PriorityQueue:
    def __init__(
        self,
        aging_rate: float = 0.1,  # priority boost per second waited
        aging_interval_s: float = 5.0,  # how often to apply aging
        max_size: int = 100_000,
    ):
        self.redis: aioredis.Redis | None = None
        self._heap: list[QueueItem] = []
        self._heap_lock = asyncio.Lock()
        self._aging_rate = aging_rate
        self._aging_interval = aging_interval_s
        self._max_size = max_size
        self._group_limits: dict[str, int] = {}  # group → max concurrent
        self._group_counts: dict[str, int] = {}
        self._dequeued: list[dict] = []
        self._stats: dict[str, int] = {
            "enqueued": 0,
            "dequeued": 0,
            "expired": 0,
            "rejected_full": 0,
            "rejected_expired": 0,
        }
        self._aging_task: asyncio.Task | None = None
        self._running = False

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def set_group_limit(self, group: str, max_concurrent: int):
        self._group_limits[group] = max_concurrent

    def _compute_sort_key(self, priority: int, aging_bonus: float = 0.0) -> float:
        # Invert: higher priority → lower key
        return -(priority + aging_bonus)

    async def enqueue(
        self,
        payload: Any,
        priority: int = Priority.NORMAL,
        group: str = "default",
        deadline_s: float = 0.0,
        tags: list[str] | None = None,
        item_id: str | None = None,
    ) -> str | None:
        async with self._heap_lock:
            if len(self._heap) >= self._max_size:
                self._stats["rejected_full"] += 1
                log.warning("Priority queue full, rejecting item")
                return None

            deadline = time.time() + deadline_s if deadline_s > 0 else 0.0
            item = QueueItem(
                sort_key=self._compute_sort_key(priority),
                item_id=item_id or str(uuid.uuid4())[:10],
                payload=payload,
                priority=priority,
                group=group,
                deadline=deadline,
                tags=tags or [],
            )

            import heapq

            heapq.heappush(self._heap, item)
            self._stats["enqueued"] += 1

            if self.redis:
                asyncio.create_task(
                    self.redis.lpush(
                        f"{REDIS_PREFIX}enqueued", json.dumps(item.to_dict())
                    )
                )
            return item.item_id

    async def dequeue(self, timeout_s: float = 0.0) -> QueueItem | None:
        import heapq

        deadline = time.time() + timeout_s if timeout_s > 0 else None

        while True:
            async with self._heap_lock:
                # Drop expired items
                while self._heap and self._heap[0].expired:
                    expired = heapq.heappop(self._heap)
                    self._stats["expired"] += 1
                    self._stats["rejected_expired"] += 1
                    log.debug(
                        f"Item {expired.item_id} expired after {expired.wait_ms:.0f}ms"
                    )

                if self._heap:
                    item = heapq.heappop(self._heap)
                    item.attempts += 1
                    self._stats["dequeued"] += 1
                    self._dequeued.append(item.to_dict())
                    if len(self._dequeued) > 1000:
                        self._dequeued = self._dequeued[-1000:]
                    return item

            if deadline and time.time() >= deadline:
                return None
            await asyncio.sleep(0.01)

    async def dequeue_batch(self, n: int, timeout_s: float = 0.1) -> list[QueueItem]:
        items = []
        deadline = time.time() + timeout_s
        while len(items) < n and time.time() < deadline:
            item = await self.dequeue(timeout_s=max(0, deadline - time.time()))
            if item:
                items.append(item)
            else:
                break
        return items

    async def requeue(self, item: QueueItem, priority_boost: int = 1):
        """Put item back with boosted priority (for retry)."""
        await self.enqueue(
            item.payload,
            priority=min(Priority.CRITICAL, item.priority + priority_boost),
            group=item.group,
            tags=item.tags,
            item_id=item.item_id,
        )

    async def _aging_loop(self):
        import heapq

        while self._running:
            await asyncio.sleep(self._aging_interval)
            async with self._heap_lock:
                now = time.time()
                for item in self._heap:
                    wait_s = now - item.enqueued_at
                    aging_bonus = wait_s * self._aging_rate
                    # Re-score with aging
                    item.sort_key = self._compute_sort_key(item.priority, aging_bonus)
                heapq.heapify(self._heap)

    async def start(self):
        self._running = True
        self._aging_task = asyncio.create_task(self._aging_loop())

    async def stop(self):
        self._running = False
        if self._aging_task:
            self._aging_task.cancel()
            try:
                await self._aging_task
            except asyncio.CancelledError:
                pass

    def peek(self) -> QueueItem | None:
        return self._heap[0] if self._heap else None

    def size(self) -> int:
        return len(self._heap)

    def size_by_group(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self._heap:
            counts[item.group] = counts.get(item.group, 0) + 1
        return counts

    def size_by_priority(self) -> dict[int, int]:
        counts: dict[int, int] = {}
        for item in self._heap:
            counts[item.priority] = counts.get(item.priority, 0) + 1
        return counts

    def stats(self) -> dict:
        return {
            **self._stats,
            "current_size": self.size(),
            "by_group": self.size_by_group(),
            "by_priority": {str(k): v for k, v in self.size_by_priority().items()},
        }


async def main():
    import sys

    q = PriorityQueue(aging_rate=0.5, aging_interval_s=1.0)
    await q.connect_redis()
    await q.start()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        # Enqueue items with different priorities
        tasks = [
            ("background_sync", Priority.BACKGROUND, "sync"),
            ("user_request_1", Priority.HIGH, "user"),
            ("cron_job", Priority.NORMAL, "cron"),
            ("critical_alert", Priority.CRITICAL, "alerts"),
            ("user_request_2", Priority.HIGH, "user"),
            ("llm_inference", Priority.NORMAL, "llm"),
            ("expiring_task", Priority.LOW, "sync"),
        ]
        for name, prio, group in tasks:
            iid = await q.enqueue(
                {"task": name},
                priority=prio,
                group=group,
                deadline_s=5.0 if "expiring" in name else 0,
            )
            print(f"  Enqueued [{iid}] {name} (p={prio})")

        print(f"\nQueue size: {q.size()}")
        print(f"By priority: {q.size_by_priority()}")

        print("\nDequeuing in priority order:")
        while q.size() > 0:
            item = await q.dequeue()
            if item:
                print(
                    f"  [{item.priority:>2}] {item.payload.get('task')} (waited {item.wait_ms:.0f}ms)"
                )

        await q.stop()
        print(f"\nStats: {json.dumps(q.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(q.stats(), indent=2))
        await q.stop()


if __name__ == "__main__":
    asyncio.run(main())
