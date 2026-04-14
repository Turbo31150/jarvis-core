#!/usr/bin/env python3
"""
jarvis_work_stealer — Work-stealing scheduler for balanced multi-worker execution
Workers steal tasks from overloaded peers to maintain even load distribution
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.work_stealer")

REDIS_PREFIX = "jarvis:ws:"
STEAL_THRESHOLD = 2  # steal if peer has this many more tasks


@dataclass
class WorkItem:
    item_id: str
    payload: Any
    priority: int = 5
    enqueued_at: float = field(default_factory=time.time)
    worker_id: str = ""
    stolen: bool = False

    @property
    def wait_ms(self) -> float:
        return (time.time() - self.enqueued_at) * 1000

    def to_dict(self) -> dict:
        return {
            "item_id": self.item_id,
            "priority": self.priority,
            "worker_id": self.worker_id,
            "stolen": self.stolen,
            "wait_ms": round(self.wait_ms, 1),
        }


@dataclass
class Worker:
    worker_id: str
    concurrency: int = 4
    queue: asyncio.deque = field(default_factory=lambda: asyncio.deque())
    active: int = 0
    processed: int = 0
    stolen_from: int = 0
    stolen_by: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def queue_size(self) -> int:
        return len(self.queue)

    @property
    def available_slots(self) -> int:
        return max(0, self.concurrency - self.active)

    def enqueue(self, item: WorkItem):
        item.worker_id = self.worker_id
        self.queue.append(item)

    def dequeue(self) -> WorkItem | None:
        return self.queue.popleft() if self.queue else None

    def steal_half(self) -> list[WorkItem]:
        """Steal half the queue (from the right end = lowest priority)."""
        n = len(self.queue) // 2
        stolen = []
        for _ in range(n):
            if self.queue:
                item = self.queue.pop()  # steal from right
                item.stolen = True
                stolen.append(item)
                self.stolen_from += 1
        return stolen

    def to_dict(self) -> dict:
        return {
            "worker_id": self.worker_id,
            "queue_size": self.queue_size,
            "active": self.active,
            "concurrency": self.concurrency,
            "processed": self.processed,
            "stolen_from": self.stolen_from,
            "stolen_by": self.stolen_by,
            "utilization": round(self.active / max(self.concurrency, 1) * 100, 1),
        }


class WorkStealer:
    def __init__(self, steal_threshold: int = STEAL_THRESHOLD):
        self.redis: aioredis.Redis | None = None
        self._workers: dict[str, Worker] = {}
        self._handler: Callable | None = None
        self._steal_threshold = steal_threshold
        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._stats: dict[str, int] = {
            "submitted": 0,
            "processed": 0,
            "steals": 0,
            "steal_rounds": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def add_worker(self, worker_id: str, concurrency: int = 4) -> Worker:
        w = Worker(worker_id=worker_id, concurrency=concurrency)
        self._workers[worker_id] = w
        return w

    def set_handler(self, fn: Callable):
        self._handler = fn

    def submit(
        self, payload: Any, priority: int = 5, worker_id: str | None = None
    ) -> str:
        item = WorkItem(
            item_id=str(uuid.uuid4())[:8], payload=payload, priority=priority
        )
        self._stats["submitted"] += 1

        if worker_id and worker_id in self._workers:
            self._workers[worker_id].enqueue(item)
        else:
            # Assign to least-loaded worker
            target = min(self._workers.values(), key=lambda w: w.queue_size + w.active)
            target.enqueue(item)

        return item.item_id

    def submit_batch(self, items: list[Any]) -> list[str]:
        return [self.submit(item) for item in items]

    def _try_steal(self, idle_worker: Worker) -> int:
        """Steal work from the most loaded peer. Returns number stolen."""
        if len(self._workers) < 2:
            return 0

        busiest = max(
            (w for w in self._workers.values() if w.worker_id != idle_worker.worker_id),
            key=lambda w: w.queue_size,
        )

        if busiest.queue_size - idle_worker.queue_size < self._steal_threshold:
            return 0

        stolen = busiest.steal_half()
        for item in stolen:
            item.worker_id = idle_worker.worker_id
            idle_worker.queue.appendleft(item)  # high priority (stolen gets LIFO)
            idle_worker.stolen_by += 1

        if stolen:
            self._stats["steals"] += len(stolen)
            log.debug(
                f"Worker {idle_worker.worker_id} stole {len(stolen)} tasks from {busiest.worker_id}"
            )

        return len(stolen)

    async def _worker_loop(self, worker: Worker):
        while self._running:
            # Try to get work
            item = worker.dequeue()
            if not item:
                # Try stealing
                self._stats["steal_rounds"] += 1
                stolen = self._try_steal(worker)
                if stolen == 0:
                    await asyncio.sleep(0.01)
                    continue
                item = worker.dequeue()
                if not item:
                    await asyncio.sleep(0.01)
                    continue

            worker.active += 1
            try:
                if self._handler:
                    if asyncio.iscoroutinefunction(self._handler):
                        await self._handler(item)
                    else:
                        self._handler(item)
                worker.processed += 1
                self._stats["processed"] += 1
            except Exception as e:
                log.error(f"Worker {worker.worker_id} handler error: {e}")
            finally:
                worker.active -= 1

    async def start(self):
        self._running = True
        for worker in self._workers.values():
            # Spawn one task per concurrency slot
            for _ in range(worker.concurrency):
                t = asyncio.create_task(self._worker_loop(worker))
                self._tasks.append(t)
        log.info(
            f"WorkStealer started: {len(self._workers)} workers, {len(self._tasks)} goroutines"
        )

    async def stop(self):
        self._running = False
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    def worker_status(self) -> list[dict]:
        return [w.to_dict() for w in self._workers.values()]

    def stats(self) -> dict:
        return {
            **self._stats,
            "workers": len(self._workers),
            "pending": sum(w.queue_size for w in self._workers.values()),
            "active": sum(w.active for w in self._workers.values()),
            "steal_efficiency": round(
                self._stats["steals"] / max(self._stats["steal_rounds"], 1), 2
            ),
        }


async def main():
    import sys

    stealer = WorkStealer(steal_threshold=2)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        processed = []

        async def handler(item: WorkItem):
            await asyncio.sleep(0.005)
            processed.append(item.item_id)

        stealer.add_worker("w1", concurrency=4)
        stealer.add_worker("w2", concurrency=4)
        stealer.add_worker("w3", concurrency=4)
        stealer.set_handler(handler)

        # Load w1 heavily
        for i in range(30):
            stealer.submit({"task": i}, worker_id="w1")
        # w2 and w3 are empty — they should steal from w1

        await stealer.start()
        await asyncio.sleep(0.5)
        await stealer.stop()

        print(f"Processed: {stealer._stats['processed']}/30")
        print(f"Steals: {stealer._stats['steals']}")
        for w in stealer.worker_status():
            print(
                f"  {w['worker_id']}: processed by workers (stolen_by={w['stolen_by']})"
            )
        print(f"\nStats: {json.dumps(stealer.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(stealer.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
