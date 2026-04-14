#!/usr/bin/env python3
"""
jarvis_async_batcher — Micro-batch accumulator for LLM requests and embeddings
Accumulates individual requests into batches for efficient bulk processing
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

log = logging.getLogger("jarvis.async_batcher")

REDIS_PREFIX = "jarvis:batcher:"


class BatchStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


@dataclass
class BatchItem:
    item_id: str
    payload: Any
    future: asyncio.Future
    priority: int = 5
    created_at: float = field(default_factory=time.time)
    deadline: float = 0.0

    @property
    def expired(self) -> bool:
        return self.deadline > 0 and time.time() > self.deadline


@dataclass
class Batch:
    batch_id: str
    items: list[BatchItem]
    status: BatchStatus = BatchStatus.PENDING
    created_at: float = field(default_factory=time.time)
    processed_at: float = 0.0
    duration_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "size": len(self.items),
            "status": self.status.value,
            "duration_ms": round(self.duration_ms, 1),
        }


@dataclass
class BatcherConfig:
    max_batch_size: int = 32
    max_wait_ms: float = 20.0  # flush after this delay even if batch not full
    max_queue_size: int = 1000
    processor: Callable | None = None  # async fn(items: list[BatchItem]) → list[Any]
    priority_enabled: bool = False


class AsyncBatcher:
    def __init__(self, config: BatcherConfig | None = None):
        self.redis: aioredis.Redis | None = None
        self._config = config or BatcherConfig()
        self._queue: list[BatchItem] = []
        self._running = False
        self._flush_event = asyncio.Event()
        self._stats: dict[str, int] = {
            "submitted": 0,
            "batches_processed": 0,
            "items_processed": 0,
            "items_dropped": 0,
            "errors": 0,
        }
        self._batch_history: list[Batch] = []

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def set_processor(self, fn: Callable):
        self._config.processor = fn

    async def submit(
        self,
        payload: Any,
        priority: int = 5,
        deadline_ms: float = 0.0,
    ) -> asyncio.Future:
        if len(self._queue) >= self._config.max_queue_size:
            self._stats["items_dropped"] += 1
            raise RuntimeError("Batcher queue full")

        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        item = BatchItem(
            item_id=str(uuid.uuid4())[:8],
            payload=payload,
            future=fut,
            priority=priority,
            deadline=time.time() + deadline_ms / 1000 if deadline_ms > 0 else 0.0,
        )
        self._queue.append(item)
        self._stats["submitted"] += 1

        # Signal to flush if batch is full
        if len(self._queue) >= self._config.max_batch_size:
            self._flush_event.set()

        return fut

    async def _flush(self):
        if not self._queue:
            return

        cfg = self._config
        # Take up to max_batch_size items (highest priority first if enabled)
        if cfg.priority_enabled:
            self._queue.sort(key=lambda i: (-i.priority, i.created_at))

        # Drop expired items
        expired = [i for i in self._queue if i.expired]
        for item in expired:
            self._queue.remove(item)
            self._stats["items_dropped"] += 1
            if not item.future.done():
                item.future.cancel()

        batch_items = self._queue[: cfg.max_batch_size]
        self._queue = self._queue[cfg.max_batch_size :]

        if not batch_items:
            return

        batch = Batch(
            batch_id=str(uuid.uuid4())[:8],
            items=batch_items,
            status=BatchStatus.PROCESSING,
        )
        t0 = time.time()
        self._stats["batches_processed"] += 1
        self._stats["items_processed"] += len(batch_items)

        try:
            if cfg.processor:
                results = await cfg.processor(batch_items)
                for item, result in zip(batch_items, results if results else []):
                    if not item.future.done():
                        item.future.set_result(result)
            else:
                # Default: return payloads as-is
                for item in batch_items:
                    if not item.future.done():
                        item.future.set_result(item.payload)

            batch.status = BatchStatus.DONE
        except Exception as e:
            batch.status = BatchStatus.FAILED
            self._stats["errors"] += 1
            for item in batch_items:
                if not item.future.done():
                    item.future.set_exception(RuntimeError(str(e)))

        batch.processed_at = time.time()
        batch.duration_ms = (batch.processed_at - t0) * 1000
        self._batch_history.append(batch)
        if len(self._batch_history) > 100:
            self._batch_history.pop(0)

    async def run(self):
        """Main batcher loop — flush on full or timeout."""
        self._running = True
        max_wait_s = self._config.max_wait_ms / 1000.0

        while self._running:
            # Wait for flush signal or timeout
            try:
                await asyncio.wait_for(self._flush_event.wait(), timeout=max_wait_s)
            except asyncio.TimeoutError:
                pass

            self._flush_event.clear()
            await self._flush()

        # Drain remaining
        while self._queue:
            await self._flush()

    def stop(self):
        self._running = False
        self._flush_event.set()

    async def submit_and_wait(
        self, payload: Any, priority: int = 5, deadline_ms: float = 0.0
    ) -> Any:
        fut = await self.submit(payload, priority, deadline_ms)
        return await fut

    def queue_depth(self) -> int:
        return len(self._queue)

    def recent_batches(self, n: int = 10) -> list[dict]:
        return [b.to_dict() for b in self._batch_history[-n:]]

    def stats(self) -> dict:
        avg_batch = self._stats["items_processed"] / max(
            self._stats["batches_processed"], 1
        )
        return {
            **self._stats,
            "queue_depth": self.queue_depth(),
            "avg_batch_size": round(avg_batch, 2),
            "max_batch_size": self._config.max_batch_size,
            "max_wait_ms": self._config.max_wait_ms,
        }


def build_embedding_batcher(
    embed_url: str = "http://192.168.1.26:1234",
) -> AsyncBatcher:
    import aiohttp

    async def embed_processor(items: list[BatchItem]) -> list[Any]:
        texts = [item.payload for item in items]
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as sess:
                async with sess.post(
                    f"{embed_url}/v1/embeddings",
                    json={"model": "nomic-embed-text", "input": texts},
                ) as r:
                    if r.status == 200:
                        data = await r.json(content_type=None)
                        return [d["embedding"] for d in data["data"]]
                    return [[] for _ in items]
        except Exception:
            return [[] for _ in items]

    cfg = BatcherConfig(max_batch_size=32, max_wait_ms=10, processor=embed_processor)
    return AsyncBatcher(cfg)


async def main():
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        # Simple pass-through batcher
        batcher = AsyncBatcher(BatcherConfig(max_batch_size=5, max_wait_ms=50))
        batcher_task = asyncio.create_task(batcher.run())

        print("Submitting 12 items in batches of 5...")
        futures = []
        for i in range(12):
            fut = await batcher.submit(f"item_{i}", priority=i % 3)
            futures.append((i, fut))
            if i % 3 == 2:
                await asyncio.sleep(0.01)

        # Wait for all
        await asyncio.sleep(0.2)
        done = sum(1 for _, f in futures if f.done() and not f.cancelled())
        print(f"Completed: {done}/12")

        print("\nRecent batches:")
        for b in batcher.recent_batches():
            print(
                f"  {b['batch_id']} size={b['size']} status={b['status']} {b['duration_ms']:.1f}ms"
            )

        batcher.stop()
        await batcher_task
        print(f"\nStats: {json.dumps(batcher.stats(), indent=2)}")

    elif cmd == "stats":
        batcher = AsyncBatcher()
        print(json.dumps(batcher.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
