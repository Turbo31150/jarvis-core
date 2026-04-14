#!/usr/bin/env python3
"""
jarvis_inference_scheduler — Priority-aware LLM inference scheduling with batching
Queues inference requests, batches compatible ones, routes to available backends
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.inference_scheduler")

REDIS_PREFIX = "jarvis:infsched:"


class InferencePriority(int, Enum):
    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3
    BACKGROUND = 4


class InferenceStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class InferenceRequest:
    req_id: str
    messages: list[dict]
    model: str
    priority: InferencePriority = InferencePriority.NORMAL
    max_tokens: int = 2048
    temperature: float = 0.7
    deadline: float = 0.0  # 0 = no deadline
    tags: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    status: InferenceStatus = InferenceStatus.QUEUED
    result: str = ""
    error: str = ""
    latency_ms: float = 0.0

    @property
    def sort_key(self) -> tuple:
        # Primary: priority, secondary: deadline (earlier first), tertiary: created_at
        dl = self.deadline if self.deadline > 0 else float("inf")
        return (self.priority.value, dl, self.created_at)

    @property
    def is_expired(self) -> bool:
        return self.deadline > 0 and time.time() > self.deadline

    def to_dict(self) -> dict:
        return {
            "req_id": self.req_id,
            "model": self.model,
            "priority": self.priority.name,
            "status": self.status.value,
            "latency_ms": round(self.latency_ms, 1),
            "error": self.error,
            "created_at": self.created_at,
        }


@dataclass
class BackendSlot:
    name: str
    url: str
    model: str
    concurrency: int = 2
    _active: int = 0

    @property
    def available(self) -> bool:
        return self._active < self.concurrency

    @property
    def load(self) -> float:
        return self._active / max(self.concurrency, 1)


class InferenceScheduler:
    def __init__(self, poll_interval_s: float = 0.05):
        self.redis: aioredis.Redis | None = None
        self._queue: list[InferenceRequest] = []
        self._in_flight: dict[str, InferenceRequest] = {}
        self._backends: list[BackendSlot] = []
        self._futures: dict[str, asyncio.Future] = {}
        self._running = False
        self._poll_interval = poll_interval_s
        self._stats: dict[str, int] = {
            "submitted": 0,
            "completed": 0,
            "failed": 0,
            "expired": 0,
            "cancelled": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def add_backend(self, slot: BackendSlot):
        self._backends.append(slot)

    def submit(
        self,
        messages: list[dict],
        model: str = "",
        priority: InferencePriority = InferencePriority.NORMAL,
        max_tokens: int = 2048,
        temperature: float = 0.7,
        deadline_s: float = 0.0,
        tags: list[str] | None = None,
    ) -> tuple[str, asyncio.Future]:
        req = InferenceRequest(
            req_id=str(uuid.uuid4())[:8],
            messages=messages,
            model=model,
            priority=priority,
            max_tokens=max_tokens,
            temperature=temperature,
            deadline=time.time() + deadline_s if deadline_s > 0 else 0.0,
            tags=tags or [],
        )
        self._queue.append(req)
        self._queue.sort(key=lambda r: r.sort_key)
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._futures[req.req_id] = fut
        self._stats["submitted"] += 1
        log.debug(f"Queued {req.req_id} priority={priority.name} model={model}")
        return req.req_id, fut

    def cancel(self, req_id: str) -> bool:
        for i, req in enumerate(self._queue):
            if req.req_id == req_id:
                req.status = InferenceStatus.CANCELLED
                self._queue.pop(i)
                self._stats["cancelled"] += 1
                fut = self._futures.pop(req_id, None)
                if fut and not fut.done():
                    fut.cancel()
                return True
        return False

    def _best_backend(self, model: str) -> BackendSlot | None:
        compatible = [
            b
            for b in self._backends
            if b.available and (not model or b.model == model or not model)
        ]
        if not compatible:
            return None
        return min(compatible, key=lambda b: b.load)

    async def _dispatch(self, req: InferenceRequest, slot: BackendSlot):
        import aiohttp

        slot._active += 1
        self._in_flight[req.req_id] = req
        req.status = InferenceStatus.RUNNING
        t0 = time.time()

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60)
            ) as sess:
                payload = {
                    "model": req.model or slot.model,
                    "messages": req.messages,
                    "max_tokens": req.max_tokens,
                    "temperature": req.temperature,
                }
                async with sess.post(
                    f"{slot.url}/v1/chat/completions", json=payload
                ) as r:
                    req.latency_ms = (time.time() - t0) * 1000
                    if r.status == 200:
                        data = await r.json(content_type=None)
                        req.result = data["choices"][0]["message"]["content"]
                        req.status = InferenceStatus.DONE
                        self._stats["completed"] += 1
                        fut = self._futures.pop(req.req_id, None)
                        if fut and not fut.done():
                            fut.set_result(req.result)
                    else:
                        raise RuntimeError(f"HTTP {r.status}")
        except Exception as e:
            req.error = str(e)[:100]
            req.status = InferenceStatus.FAILED
            req.latency_ms = (time.time() - t0) * 1000
            self._stats["failed"] += 1
            fut = self._futures.pop(req.req_id, None)
            if fut and not fut.done():
                fut.set_exception(RuntimeError(req.error))
        finally:
            slot._active -= 1
            self._in_flight.pop(req.req_id, None)

    async def _tick(self):
        expired = [r for r in self._queue if r.is_expired]
        for req in expired:
            self._queue.remove(req)
            req.status = InferenceStatus.CANCELLED
            self._stats["expired"] += 1
            fut = self._futures.pop(req.req_id, None)
            if fut and not fut.done():
                fut.cancel()

        while self._queue:
            req = self._queue[0]
            slot = self._best_backend(req.model)
            if not slot:
                break
            self._queue.pop(0)
            asyncio.create_task(self._dispatch(req, slot))

    async def run(self):
        self._running = True
        while self._running:
            await self._tick()
            await asyncio.sleep(self._poll_interval)

    def stop(self):
        self._running = False

    def queue_depth(self) -> dict[str, int]:
        by_priority: dict[str, int] = {}
        for req in self._queue:
            k = req.priority.name
            by_priority[k] = by_priority.get(k, 0) + 1
        return by_priority

    def stats(self) -> dict:
        return {
            **self._stats,
            "queue_depth": len(self._queue),
            "in_flight": len(self._in_flight),
            "backends": len(self._backends),
            "by_priority": self.queue_depth(),
        }


def build_jarvis_scheduler() -> InferenceScheduler:
    sched = InferenceScheduler()
    sched.add_backend(
        BackendSlot("m1", "http://192.168.1.85:1234", "qwen3.5-9b", concurrency=3)
    )
    sched.add_backend(
        BackendSlot("m2", "http://192.168.1.26:1234", "qwen3.5-9b", concurrency=2)
    )
    sched.add_backend(
        BackendSlot("ol1", "http://127.0.0.1:11434", "gemma3:4b", concurrency=1)
    )
    return sched


async def main():
    import sys

    sched = build_jarvis_scheduler()
    await sched.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        # Start scheduler
        scheduler_task = asyncio.create_task(sched.run())

        # Submit requests at different priorities
        messages = [{"role": "user", "content": "Say hello in one word."}]
        req_id1, fut1 = sched.submit(
            messages, priority=InferencePriority.HIGH, deadline_s=10
        )
        req_id2, fut2 = sched.submit(
            messages, priority=InferencePriority.NORMAL, deadline_s=10
        )
        print(f"Submitted: {req_id1}, {req_id2}")
        print(f"Queue depth: {sched.queue_depth()}")

        # Wait briefly then check stats
        await asyncio.sleep(2)
        print(f"\nStats: {json.dumps(sched.stats(), indent=2)}")
        sched.stop()
        scheduler_task.cancel()

    elif cmd == "stats":
        print(json.dumps(sched.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
