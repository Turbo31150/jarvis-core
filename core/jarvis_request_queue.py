#!/usr/bin/env python3
"""
jarvis_request_queue — Priority request queue with fairness, deadlines, and backpressure
Multi-producer/consumer async queue with per-agent fairness slots
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.request_queue")

REDIS_PREFIX = "jarvis:rq:"


class QueuePriority(int, Enum):
    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3
    BACKGROUND = 4


class RequestStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


@dataclass
class QueueRequest:
    req_id: str
    agent_id: str
    payload: Any
    priority: QueuePriority = QueuePriority.NORMAL
    deadline: float = 0.0  # 0 = no deadline
    enqueued_at: float = field(default_factory=time.time)
    status: RequestStatus = RequestStatus.QUEUED
    started_at: float = 0.0
    finished_at: float = 0.0
    result: Any = None
    error: str = ""
    _future: asyncio.Future | None = field(default=None, repr=False)

    @property
    def age_s(self) -> float:
        return time.time() - self.enqueued_at

    @property
    def is_expired(self) -> bool:
        return self.deadline > 0 and time.time() > self.deadline

    @property
    def wait_ms(self) -> float:
        if self.started_at:
            return (self.started_at - self.enqueued_at) * 1000
        return self.age_s * 1000

    def to_dict(self) -> dict:
        return {
            "req_id": self.req_id,
            "agent_id": self.agent_id,
            "priority": self.priority.value,
            "status": self.status.value,
            "age_s": round(self.age_s, 2),
            "wait_ms": round(self.wait_ms, 2),
            "deadline": self.deadline,
            "error": self.error[:100] if self.error else "",
        }


class RequestQueue:
    def __init__(
        self,
        max_size: int = 10_000,
        fairness_window: int = 10,  # max consecutive requests per agent
    ):
        self.redis: aioredis.Redis | None = None
        # Priority buckets: list per priority level
        self._buckets: dict[int, list[QueueRequest]] = {
            p.value: [] for p in QueuePriority
        }
        self._in_flight: dict[str, QueueRequest] = {}
        self._max_size = max_size
        self._fairness_window = fairness_window
        self._agent_counts: dict[str, int] = {}  # recent consecutive per agent
        self._last_agent: str = ""
        self._stats: dict[str, int] = {
            "enqueued": 0,
            "dequeued": 0,
            "expired": 0,
            "cancelled": 0,
            "completed": 0,
            "failed": 0,
        }
        self._size = 0
        self._notify = asyncio.Event()

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def _total_queued(self) -> int:
        return sum(len(b) for b in self._buckets.values())

    def enqueue(
        self,
        agent_id: str,
        payload: Any,
        priority: QueuePriority = QueuePriority.NORMAL,
        deadline_s: float = 0.0,
        wait_result: bool = False,
    ) -> QueueRequest:
        if self._total_queued() >= self._max_size:
            raise OverflowError(f"Queue full ({self._max_size})")

        req = QueueRequest(
            req_id=str(uuid.uuid4())[:10],
            agent_id=agent_id,
            payload=payload,
            priority=priority,
            deadline=time.time() + deadline_s if deadline_s > 0 else 0.0,
        )
        if wait_result:
            req._future = asyncio.get_event_loop().create_future()

        self._buckets[priority.value].append(req)
        self._stats["enqueued"] += 1
        self._notify.set()

        log.debug(f"Enqueued req={req.req_id} agent={agent_id} prio={priority.name}")
        return req

    def _pick_next(self) -> QueueRequest | None:
        """Pick next request respecting priority and fairness."""
        # Purge expired entries first
        now = time.time()
        for bucket in self._buckets.values():
            expired = [r for r in bucket if r.deadline > 0 and now > r.deadline]
            for r in expired:
                bucket.remove(r)
                r.status = RequestStatus.EXPIRED
                self._stats["expired"] += 1
                if r._future and not r._future.done():
                    r._future.set_exception(TimeoutError("Request deadline exceeded"))

        # Pick from highest priority non-empty bucket
        for prio in sorted(self._buckets.keys()):
            bucket = self._buckets[prio]
            if not bucket:
                continue

            # Fairness: skip if agent has had too many consecutive slots
            # Try to find a request from a different agent first
            for i, req in enumerate(bucket):
                consecutive = self._agent_counts.get(req.agent_id, 0)
                if (
                    req.agent_id != self._last_agent
                    or consecutive < self._fairness_window
                ):
                    bucket.pop(i)
                    return req

            # All from same agent — allow anyway
            req = bucket.pop(0)
            return req

        return None

    async def dequeue(self, timeout_s: float = 30.0) -> QueueRequest | None:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            req = self._pick_next()
            if req:
                req.status = RequestStatus.PROCESSING
                req.started_at = time.time()
                self._in_flight[req.req_id] = req
                self._stats["dequeued"] += 1
                # Update fairness tracking
                if req.agent_id == self._last_agent:
                    self._agent_counts[req.agent_id] = (
                        self._agent_counts.get(req.agent_id, 0) + 1
                    )
                else:
                    self._agent_counts[self._last_agent] = 0
                    self._last_agent = req.agent_id
                return req

            self._notify.clear()
            try:
                await asyncio.wait_for(
                    self._notify.wait(), timeout=min(1.0, deadline - time.time())
                )
            except asyncio.TimeoutError:
                pass

        return None

    def complete(self, req_id: str, result: Any = None):
        req = self._in_flight.pop(req_id, None)
        if not req:
            return
        req.status = RequestStatus.DONE
        req.finished_at = time.time()
        req.result = result
        self._stats["completed"] += 1
        if req._future and not req._future.done():
            req._future.set_result(result)

    def fail(self, req_id: str, error: str = ""):
        req = self._in_flight.pop(req_id, None)
        if not req:
            return
        req.status = RequestStatus.FAILED
        req.finished_at = time.time()
        req.error = error
        self._stats["failed"] += 1
        if req._future and not req._future.done():
            req._future.set_exception(RuntimeError(error))

    def cancel(self, req_id: str):
        for bucket in self._buckets.values():
            for r in bucket:
                if r.req_id == req_id:
                    bucket.remove(r)
                    r.status = RequestStatus.CANCELLED
                    self._stats["cancelled"] += 1
                    if r._future and not r._future.done():
                        r._future.cancel()
                    return

    def queue_depth(self) -> dict[str, int]:
        return {QueuePriority(p).name: len(b) for p, b in self._buckets.items()}

    def stats(self) -> dict:
        return {
            **self._stats,
            "queued": self._total_queued(),
            "in_flight": len(self._in_flight),
            "depth": self.queue_depth(),
        }


def build_jarvis_request_queue(max_size: int = 10_000) -> RequestQueue:
    return RequestQueue(max_size=max_size, fairness_window=10)


async def main():
    import sys
    import random

    q = build_jarvis_request_queue()
    await q.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Enqueuing requests...")
        agents = ["trading-agent", "monitor", "inference-gw", "admin"]
        reqs = []
        for i in range(12):
            agent = agents[i % len(agents)]
            prio = random.choice(list(QueuePriority))
            r = q.enqueue(agent, {"task": f"work-{i}"}, priority=prio, deadline_s=10.0)
            reqs.append(r)
            print(f"  Enqueued {r.req_id} agent={agent} prio={prio.name}")

        print("\nDequeuing in priority order:")
        for _ in range(6):
            req = await q.dequeue(timeout_s=0.1)
            if req:
                print(
                    f"  Got {req.req_id} agent={req.agent_id} prio={QueuePriority(req.priority).name}"
                )
                q.complete(req.req_id, result="ok")

        print(f"\nQueue depth: {q.queue_depth()}")
        print(f"Stats: {json.dumps(q.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(q.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
