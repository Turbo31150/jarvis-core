#!/usr/bin/env python3
"""
jarvis_job_queue — Persistent async job queue with retry and dead-letter
Redis-backed job queue with at-least-once delivery, retry backoff, and DLQ
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

log = logging.getLogger("jarvis.job_queue")

REDIS_PREFIX = "jarvis:jq:"
DLQ_FILE = Path("/home/turbo/IA/Core/jarvis/data/job_dlq.jsonl")


class JobStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
    DEAD = "dead"  # in DLQ


@dataclass
class Job:
    job_id: str
    queue: str
    payload: Any
    status: JobStatus = JobStatus.PENDING
    priority: int = 5
    max_retries: int = 3
    retries: int = 0
    retry_delay_s: float = 5.0
    scheduled_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    ended_at: float = 0.0
    error: str = ""
    worker: str = ""
    tags: list[str] = field(default_factory=list)

    @property
    def duration_ms(self) -> float:
        if self.started_at and self.ended_at:
            return round((self.ended_at - self.started_at) * 1000, 1)
        return 0.0

    @property
    def next_retry_at(self) -> float:
        # Exponential backoff: delay * 2^retries
        return time.time() + self.retry_delay_s * (2**self.retries)

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "queue": self.queue,
            "status": self.status.value,
            "priority": self.priority,
            "retries": self.retries,
            "max_retries": self.max_retries,
            "duration_ms": self.duration_ms,
            "error": self.error[:200] if self.error else "",
            "worker": self.worker,
            "tags": self.tags,
        }


class JobQueue:
    def __init__(self, worker_id: str | None = None):
        self.redis: aioredis.Redis | None = None
        self.worker_id = worker_id or str(uuid.uuid4())[:8]
        self._handlers: dict[str, Callable] = {}
        self._jobs: dict[str, Job] = {}
        self._running = False
        self._consumer_tasks: list[asyncio.Task] = []
        self._stats: dict[str, int] = {
            "enqueued": 0,
            "processed": 0,
            "failed": 0,
            "retried": 0,
            "dead_lettered": 0,
        }
        DLQ_FILE.parent.mkdir(parents=True, exist_ok=True)

    async def connect_redis(self):
        self.redis = aioredis.Redis(decode_responses=True)
        await self.redis.ping()

    def register(self, queue: str, handler: Callable):
        self._handlers[queue] = handler

    async def enqueue(
        self,
        queue: str,
        payload: Any,
        priority: int = 5,
        delay_s: float = 0.0,
        max_retries: int = 3,
        retry_delay_s: float = 5.0,
        tags: list[str] | None = None,
        job_id: str | None = None,
    ) -> str:
        job = Job(
            job_id=job_id or str(uuid.uuid4())[:10],
            queue=queue,
            payload=payload,
            priority=priority,
            max_retries=max_retries,
            retry_delay_s=retry_delay_s,
            scheduled_at=time.time() + delay_s,
            tags=tags or [],
        )
        self._jobs[job.job_id] = job
        self._stats["enqueued"] += 1

        if self.redis:
            score = job.scheduled_at - priority * 0.001  # lower score = earlier
            await self.redis.zadd(
                f"{REDIS_PREFIX}queue:{queue}",
                {json.dumps(job.to_dict()): score},
            )
        return job.job_id

    async def enqueue_batch(
        self, queue: str, payloads: list[Any], **kwargs
    ) -> list[str]:
        return [await self.enqueue(queue, p, **kwargs) for p in payloads]

    async def _fetch_job(self, queue: str) -> Job | None:
        if not self.redis:
            return None
        now = time.time()
        # Get jobs ready to run (scheduled_at <= now)
        items = await self.redis.zrangebyscore(
            f"{REDIS_PREFIX}queue:{queue}",
            min="-inf",
            max=now,
            start=0,
            num=1,
            withscores=False,
        )
        if not items:
            return None

        # Atomically remove and claim
        raw = items[0]
        removed = await self.redis.zrem(f"{REDIS_PREFIX}queue:{queue}", raw)
        if not removed:
            return None

        try:
            data = json.loads(raw)
            job = Job(
                job_id=data["job_id"],
                queue=data["queue"],
                payload=data.get("payload", {}),
                priority=data.get("priority", 5),
                max_retries=data.get("max_retries", 3),
                retries=data.get("retries", 0),
                retry_delay_s=data.get("retry_delay_s", 5.0),
                tags=data.get("tags", []),
            )
            return job
        except Exception as e:
            log.warning(f"Failed to parse job: {e}")
            return None

    async def _process_job(self, job: Job):
        handler = self._handlers.get(job.queue)
        if not handler:
            log.warning(f"No handler for queue '{job.queue}'")
            return

        job.status = JobStatus.PROCESSING
        job.started_at = time.time()
        job.worker = self.worker_id

        try:
            if asyncio.iscoroutinefunction(handler):
                await handler(job)
            else:
                handler(job)
            job.status = JobStatus.DONE
            job.ended_at = time.time()
            self._stats["processed"] += 1
            log.debug(f"Job {job.job_id} done in {job.duration_ms:.0f}ms")

        except Exception as e:
            job.error = str(e)[:300]
            job.ended_at = time.time()
            self._stats["failed"] += 1

            if job.retries < job.max_retries:
                job.retries += 1
                job.status = JobStatus.PENDING
                self._stats["retried"] += 1
                delay = job.retry_delay_s * (2 ** (job.retries - 1))
                log.warning(
                    f"Job {job.job_id} failed, retry {job.retries}/{job.max_retries} in {delay:.1f}s"
                )
                await self.enqueue(
                    job.queue,
                    job.payload,
                    priority=job.priority,
                    delay_s=delay,
                    max_retries=job.max_retries,
                    retry_delay_s=job.retry_delay_s,
                    job_id=job.job_id,
                )
            else:
                job.status = JobStatus.DEAD
                self._stats["dead_lettered"] += 1
                log.error(
                    f"Job {job.job_id} dead-lettered after {job.max_retries} retries"
                )
                with open(DLQ_FILE, "a") as f:
                    f.write(json.dumps(job.to_dict()) + "\n")

        self._jobs[job.job_id] = job

    async def _consumer_loop(self, queues: list[str], poll_interval_s: float = 0.5):
        while self._running:
            found = False
            for queue in queues:
                job = await self._fetch_job(queue)
                if job:
                    asyncio.create_task(self._process_job(job))
                    found = True
            if not found:
                await asyncio.sleep(poll_interval_s)

    async def start(self, queues: list[str], concurrency: int = 4, poll_s: float = 0.5):
        self._running = True
        for _ in range(concurrency):
            t = asyncio.create_task(self._consumer_loop(queues, poll_s))
            self._consumer_tasks.append(t)
        log.info(f"JobQueue started: queues={queues} concurrency={concurrency}")

    async def stop(self):
        self._running = False
        for t in self._consumer_tasks:
            t.cancel()
        await asyncio.gather(*self._consumer_tasks, return_exceptions=True)

    def get_job(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    async def queue_depth(self, queue: str) -> int:
        if not self.redis:
            return 0
        return await self.redis.zcard(f"{REDIS_PREFIX}queue:{queue}")

    def stats(self) -> dict:
        return {
            **self._stats,
            "worker_id": self.worker_id,
            "tracked_jobs": len(self._jobs),
            "handlers": list(self._handlers.keys()),
        }


async def main():
    import sys

    jq = JobQueue(worker_id="worker-1")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    try:
        await jq.connect_redis()
    except Exception as e:
        print(f"Redis unavailable: {e}")
        return

    if cmd == "demo":
        results = []

        async def process_task(job: Job):
            await asyncio.sleep(0.02)
            if job.payload.get("fail") and job.retries < 1:
                raise ValueError("Simulated failure")
            results.append(job.job_id)

        jq.register("tasks", process_task)

        # Submit jobs
        ids = []
        for i in range(5):
            jid = await jq.enqueue("tasks", {"index": i})
            ids.append(jid)
        fail_jid = await jq.enqueue(
            "tasks", {"fail": True}, max_retries=2, retry_delay_s=0.1
        )
        ids.append(fail_jid)

        depth = await jq.queue_depth("tasks")
        print(f"Queue depth: {depth}")

        await jq.start(["tasks"], concurrency=2)
        await asyncio.sleep(1.5)
        await jq.stop()

        print(f"Processed: {len(results)}/{len(ids)}")
        print(f"Stats: {json.dumps(jq.stats(), indent=2)}")

    elif cmd == "depth" and len(sys.argv) > 2:
        d = await jq.queue_depth(sys.argv[2])
        print(f"Queue '{sys.argv[2]}' depth: {d}")

    elif cmd == "stats":
        print(json.dumps(jq.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
