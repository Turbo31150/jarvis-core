#!/usr/bin/env python3
"""
jarvis_priority_scheduler — Priority-based task scheduler with preemption and SLA tracking
Deadline-aware scheduling, priority aging, multi-queue dispatch
"""

import asyncio
import heapq
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.priority_scheduler")

REDIS_PREFIX = "jarvis:psched:"


class TaskPriority(int, Enum):
    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3
    BACKGROUND = 4


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


@dataclass
class ScheduledTask:
    task_id: str
    name: str
    priority: TaskPriority
    fn: Callable
    args: dict[str, Any] = field(default_factory=dict)
    deadline: float = 0.0  # 0 = no deadline
    max_runtime_s: float = 60.0
    status: TaskStatus = TaskStatus.QUEUED
    enqueued_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    result: Any = None
    error: str = ""
    agent: str = ""  # affinity — preferred worker
    _effective_priority: float = 0.0  # priority after aging

    def __lt__(self, other: "ScheduledTask") -> bool:
        return self._effective_priority < other._effective_priority

    @property
    def wait_s(self) -> float:
        return time.time() - self.enqueued_at

    @property
    def is_expired(self) -> bool:
        return self.deadline > 0 and time.time() > self.deadline

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "name": self.name,
            "priority": self.priority.name,
            "status": self.status.value,
            "wait_s": round(self.wait_s, 2),
            "enqueued_at": self.enqueued_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "deadline": self.deadline,
            "agent": self.agent,
        }


class PriorityScheduler:
    def __init__(
        self,
        workers: int = 4,
        aging_rate: float = 0.1,  # priority boost per second waiting
        age_interval_s: float = 5.0,
    ):
        self.redis: aioredis.Redis | None = None
        self._workers = workers
        self._aging_rate = aging_rate
        self._age_interval = age_interval_s

        self._heap: list[ScheduledTask] = []  # min-heap
        self._tasks: dict[str, ScheduledTask] = {}
        self._running: dict[str, asyncio.Task] = {}
        self._semaphore: asyncio.Semaphore | None = None

        self._running_flag = False
        self._loop_task: asyncio.Task | None = None
        self._age_task: asyncio.Task | None = None
        self._done_callbacks: list[Callable] = []

        self._stats: dict[str, int] = {
            "enqueued": 0,
            "dispatched": 0,
            "done": 0,
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

    def on_done(self, callback: Callable):
        self._done_callbacks.append(callback)

    def _fire_done(self, task: ScheduledTask):
        for cb in self._done_callbacks:
            try:
                cb(task)
            except Exception:
                pass

    def enqueue(
        self,
        name: str,
        fn: Callable,
        args: dict | None = None,
        priority: TaskPriority = TaskPriority.NORMAL,
        deadline: float = 0.0,
        max_runtime_s: float = 60.0,
        agent: str = "",
    ) -> str:
        import secrets

        task_id = secrets.token_hex(8)
        task = ScheduledTask(
            task_id=task_id,
            name=name,
            priority=priority,
            fn=fn,
            args=args or {},
            deadline=deadline,
            max_runtime_s=max_runtime_s,
            agent=agent,
            _effective_priority=float(priority.value),
        )
        self._tasks[task_id] = task
        heapq.heappush(self._heap, task)
        self._stats["enqueued"] += 1
        log.debug(f"Enqueued {name!r} [{task_id}] priority={priority.name}")
        return task_id

    def cancel(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if not task:
            return False
        if task.status == TaskStatus.QUEUED:
            task.status = TaskStatus.CANCELLED
            self._stats["cancelled"] += 1
            return True
        if task.status == TaskStatus.RUNNING:
            running = self._running.get(task_id)
            if running:
                running.cancel()
            task.status = TaskStatus.CANCELLED
            self._stats["cancelled"] += 1
            return True
        return False

    async def _execute(self, task: ScheduledTask):
        task.status = TaskStatus.RUNNING
        task.started_at = time.time()
        self._stats["dispatched"] += 1

        try:
            coro = task.fn(**task.args)
            if asyncio.iscoroutine(coro):
                result = await asyncio.wait_for(coro, timeout=task.max_runtime_s)
            else:
                result = coro
            task.result = result
            task.status = TaskStatus.DONE
            task.finished_at = time.time()
            self._stats["done"] += 1
            log.debug(
                f"Task {task.name!r} [{task.task_id}] done in {task.finished_at - task.started_at:.2f}s"
            )
        except asyncio.TimeoutError:
            task.status = TaskStatus.FAILED
            task.error = f"timeout after {task.max_runtime_s}s"
            task.finished_at = time.time()
            self._stats["failed"] += 1
        except asyncio.CancelledError:
            task.status = TaskStatus.CANCELLED
            task.finished_at = time.time()
            self._stats["cancelled"] += 1
        except Exception as e:
            task.status = TaskStatus.FAILED
            task.error = str(e)
            task.finished_at = time.time()
            self._stats["failed"] += 1
            log.error(f"Task {task.name!r} [{task.task_id}] failed: {e}")
        finally:
            self._running.pop(task.task_id, None)
            self._fire_done(task)

    async def _dispatch_loop(self):
        assert self._semaphore is not None
        while self._running_flag:
            # Drain expired entries
            while self._heap:
                top = self._heap[0]
                if top.status != TaskStatus.QUEUED:
                    heapq.heappop(self._heap)
                    continue
                if top.is_expired:
                    heapq.heappop(self._heap)
                    top.status = TaskStatus.EXPIRED
                    top.finished_at = time.time()
                    self._stats["expired"] += 1
                    log.warning(f"Task {top.name!r} expired")
                    self._fire_done(top)
                    continue
                break

            if not self._heap:
                await asyncio.sleep(0.05)
                continue

            # Try to acquire worker slot
            acquired = self._semaphore._value > 0
            if not acquired:
                await asyncio.sleep(0.05)
                continue

            await self._semaphore.acquire()
            if not self._heap:
                self._semaphore.release()
                await asyncio.sleep(0.05)
                continue

            task = heapq.heappop(self._heap)
            if task.status != TaskStatus.QUEUED:
                self._semaphore.release()
                continue

            async def _run(t: ScheduledTask):
                try:
                    await self._execute(t)
                finally:
                    self._semaphore.release()

            t = asyncio.create_task(_run(task))
            self._running[task.task_id] = t

    async def _aging_loop(self):
        while self._running_flag:
            await asyncio.sleep(self._age_interval)
            now = time.time()
            for task in self._heap:
                if task.status == TaskStatus.QUEUED:
                    # Boost priority (lower = higher) by aging
                    age = now - task.enqueued_at
                    task._effective_priority = float(task.priority.value) - (
                        age * self._aging_rate
                    )
            heapq.heapify(self._heap)

    def start(self):
        self._running_flag = True
        self._semaphore = asyncio.Semaphore(self._workers)
        self._loop_task = asyncio.create_task(self._dispatch_loop())
        self._age_task = asyncio.create_task(self._aging_loop())
        log.info(f"PriorityScheduler started workers={self._workers}")

    async def stop(self):
        self._running_flag = False
        for t in self._running.values():
            t.cancel()
        if self._loop_task:
            self._loop_task.cancel()
        if self._age_task:
            self._age_task.cancel()
        await asyncio.gather(
            *list(self._running.values()),
            *(t for t in [self._loop_task, self._age_task] if t),
            return_exceptions=True,
        )

    def queue_depth(self, priority: TaskPriority | None = None) -> int:
        tasks = [t for t in self._heap if t.status == TaskStatus.QUEUED]
        if priority is not None:
            tasks = [t for t in tasks if t.priority == priority]
        return len(tasks)

    def running_count(self) -> int:
        return len(self._running)

    def get_task(self, task_id: str) -> ScheduledTask | None:
        return self._tasks.get(task_id)

    def list_queued(self, limit: int = 20) -> list[dict]:
        queued = sorted(
            [t for t in self._heap if t.status == TaskStatus.QUEUED],
            key=lambda t: t._effective_priority,
        )
        return [t.to_dict() for t in queued[:limit]]

    def stats(self) -> dict:
        return {
            **self._stats,
            "queued": self.queue_depth(),
            "running": self.running_count(),
            "total_tasks": len(self._tasks),
            "workers": self._workers,
        }


def build_jarvis_priority_scheduler(workers: int = 4) -> PriorityScheduler:
    return PriorityScheduler(workers=workers, aging_rate=0.05, age_interval_s=2.0)


async def main():
    import sys

    sched = build_jarvis_priority_scheduler(workers=2)
    await sched.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Priority scheduler demo...")
        sched.start()

        results: list[str] = []

        async def work(name: str, delay: float = 0.05):
            await asyncio.sleep(delay)
            results.append(name)
            return {"done": name}

        # Enqueue mixed priorities
        sched.enqueue(
            "bg-task-1", work, {"name": "bg-1", "delay": 0.1}, TaskPriority.BACKGROUND
        )
        sched.enqueue(
            "bg-task-2", work, {"name": "bg-2", "delay": 0.1}, TaskPriority.BACKGROUND
        )
        sched.enqueue(
            "normal-1", work, {"name": "n-1", "delay": 0.05}, TaskPriority.NORMAL
        )
        sched.enqueue("high-1", work, {"name": "h-1", "delay": 0.02}, TaskPriority.HIGH)
        sched.enqueue(
            "critical-1", work, {"name": "c-1", "delay": 0.01}, TaskPriority.CRITICAL
        )

        # Deadline-expired task
        sched.enqueue(
            "expired-task",
            work,
            {"name": "exp", "delay": 0.01},
            TaskPriority.LOW,
            deadline=time.time() - 1,
        )

        await asyncio.sleep(0.5)
        await sched.stop()

        print(f"\n  Execution order: {results}")
        print(f"\nStats: {json.dumps(sched.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(sched.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
