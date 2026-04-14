#!/usr/bin/env python3
"""
jarvis_task_dispatcher_v2 — Priority-aware async task dispatcher
Worker pools, priority queues, task routing by type, dead-letter queue
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

log = logging.getLogger("jarvis.task_dispatcher_v2")

REDIS_PREFIX = "jarvis:dispatch2:"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DEAD = "dead"


class Priority(int, Enum):
    LOW = 1
    NORMAL = 5
    HIGH = 8
    CRITICAL = 10


@dataclass
class Task:
    task_id: str
    task_type: str
    payload: dict
    priority: Priority = Priority.NORMAL
    status: TaskStatus = TaskStatus.PENDING
    max_retries: int = 3
    retries: int = 0
    timeout_s: float = 60.0
    result: Any = None
    error: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    ended_at: float = 0.0
    worker_id: str = ""

    @property
    def duration_ms(self) -> float:
        if self.started_at and self.ended_at:
            return round((self.ended_at - self.started_at) * 1000, 1)
        return 0.0

    @property
    def wait_ms(self) -> float:
        if self.started_at:
            return round((self.started_at - self.created_at) * 1000, 1)
        return round((time.time() - self.created_at) * 1000, 1)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "priority": self.priority.value,
            "status": self.status.value,
            "retries": self.retries,
            "max_retries": self.max_retries,
            "result": str(self.result)[:200] if self.result is not None else None,
            "error": self.error[:200],
            "duration_ms": self.duration_ms,
            "wait_ms": self.wait_ms,
            "worker_id": self.worker_id,
        }


@dataclass
class Worker:
    worker_id: str
    task_types: list[str]  # empty = handle all
    concurrency: int = 1
    active_tasks: int = 0
    total_done: int = 0
    total_failed: int = 0
    started_at: float = field(default_factory=time.time)

    @property
    def available(self) -> bool:
        return self.active_tasks < self.concurrency

    def handles(self, task_type: str) -> bool:
        return not self.task_types or task_type in self.task_types

    def to_dict(self) -> dict:
        return {
            "worker_id": self.worker_id,
            "task_types": self.task_types,
            "concurrency": self.concurrency,
            "active_tasks": self.active_tasks,
            "total_done": self.total_done,
            "total_failed": self.total_failed,
        }


class TaskDispatcherV2:
    def __init__(self, default_concurrency: int = 4):
        self.redis: aioredis.Redis | None = None
        self.default_concurrency = default_concurrency
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._tasks: dict[str, Task] = {}
        self._workers: dict[str, Worker] = {}
        self._handlers: dict[str, Callable] = {}
        self._dlq: list[Task] = []  # dead-letter queue
        self._dispatch_task: asyncio.Task | None = None
        self._stats: dict[str, int] = {
            "submitted": 0,
            "completed": 0,
            "failed": 0,
            "retried": 0,
            "dead": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def register_handler(self, task_type: str, fn: Callable):
        self._handlers[task_type] = fn
        log.debug(f"Handler registered: {task_type}")

    def add_worker(
        self, worker_id: str, task_types: list[str] | None = None, concurrency: int = 1
    ) -> Worker:
        w = Worker(
            worker_id=worker_id, task_types=task_types or [], concurrency=concurrency
        )
        self._workers[worker_id] = w
        return w

    async def submit(
        self,
        task_type: str,
        payload: dict,
        priority: Priority = Priority.NORMAL,
        timeout_s: float = 60.0,
        max_retries: int = 3,
        task_id: str | None = None,
    ) -> Task:
        task = Task(
            task_id=task_id or str(uuid.uuid4())[:8],
            task_type=task_type,
            payload=payload,
            priority=priority,
            timeout_s=timeout_s,
            max_retries=max_retries,
        )
        self._tasks[task.task_id] = task
        # PriorityQueue: lower number = higher priority → negate
        await self._queue.put((-priority.value, task.created_at, task.task_id))
        self._stats["submitted"] += 1

        if self.redis:
            await self.redis.lpush(
                f"{REDIS_PREFIX}submitted", json.dumps(task.to_dict())
            )
            await self.redis.ltrim(f"{REDIS_PREFIX}submitted", 0, 999)

        log.debug(
            f"Task submitted: [{task.task_id}] {task_type} priority={priority.name}"
        )
        return task

    async def submit_many(self, tasks: list[dict]) -> list[Task]:
        return await asyncio.gather(
            *[
                self.submit(
                    t["task_type"],
                    t.get("payload", {}),
                    Priority(t.get("priority", Priority.NORMAL)),
                    t.get("timeout_s", 60.0),
                )
                for t in tasks
            ]
        )

    def _pick_worker(self, task_type: str) -> Worker | None:
        available = [
            w for w in self._workers.values() if w.available and w.handles(task_type)
        ]
        if not available:
            return None
        return min(available, key=lambda w: w.active_tasks)

    async def _execute(self, task: Task, worker: Worker):
        handler = self._handlers.get(task.task_type) or self._handlers.get("*")
        if not handler:
            task.status = TaskStatus.FAILED
            task.error = f"No handler for task_type '{task.task_type}'"
            worker.total_failed += 1
            self._stats["failed"] += 1
            return

        task.status = TaskStatus.RUNNING
        task.started_at = time.time()
        task.worker_id = worker.worker_id
        worker.active_tasks += 1

        try:
            if asyncio.iscoroutinefunction(handler):
                coro = handler(task)
                task.result = await asyncio.wait_for(coro, timeout=task.timeout_s)
            else:
                task.result = handler(task)

            task.status = TaskStatus.DONE
            task.ended_at = time.time()
            worker.total_done += 1
            self._stats["completed"] += 1
            log.debug(f"Task done: [{task.task_id}] in {task.duration_ms:.0f}ms")

        except asyncio.TimeoutError:
            task.error = f"Timeout after {task.timeout_s}s"
            await self._handle_failure(task, worker)
        except Exception as e:
            task.error = str(e)
            await self._handle_failure(task, worker)
        finally:
            worker.active_tasks -= 1
            task.ended_at = task.ended_at or time.time()

        if self.redis:
            await self.redis.setex(
                f"{REDIS_PREFIX}task:{task.task_id}",
                3600,
                json.dumps(task.to_dict()),
            )

    async def _handle_failure(self, task: Task, worker: Worker):
        task.retries += 1
        if task.retries <= task.max_retries:
            task.status = TaskStatus.PENDING
            delay = 2 ** (task.retries - 1)
            self._stats["retried"] += 1
            log.warning(
                f"Task retrying: [{task.task_id}] attempt {task.retries} in {delay}s"
            )
            await asyncio.sleep(delay)
            await self._queue.put((-task.priority.value, task.created_at, task.task_id))
        else:
            task.status = TaskStatus.DEAD
            self._dlq.append(task)
            worker.total_failed += 1
            self._stats["failed"] += 1
            self._stats["dead"] += 1
            log.error(
                f"Task dead: [{task.task_id}] after {task.retries} retries: {task.error}"
            )

    async def _dispatch_loop(self):
        while True:
            try:
                _, _, task_id = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                task = self._tasks.get(task_id)
                if not task or task.status == TaskStatus.CANCELLED:
                    continue

                worker = self._pick_worker(task.task_type)
                if not worker:
                    # No worker available — requeue
                    await self._queue.put(
                        (-task.priority.value, task.created_at, task_id)
                    )
                    await asyncio.sleep(0.05)
                    continue

                asyncio.create_task(self._execute(task, worker))
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                log.error(f"Dispatch error: {e}")

    async def start(self):
        if not self._workers:
            self.add_worker("default", concurrency=self.default_concurrency)
        self._dispatch_task = asyncio.create_task(self._dispatch_loop())
        log.info(f"Dispatcher started: {len(self._workers)} workers")

    def stop(self):
        if self._dispatch_task:
            self._dispatch_task.cancel()

    def cancel(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task and task.status == TaskStatus.PENDING:
            task.status = TaskStatus.CANCELLED
            return True
        return False

    async def wait(self, task_id: str, timeout_s: float = 60.0) -> Task:
        deadline = time.time() + timeout_s
        task = self._tasks.get(task_id)
        if not task:
            raise ValueError(f"Task '{task_id}' not found")
        while time.time() < deadline:
            if task.status in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.DEAD):
                return task
            await asyncio.sleep(0.1)
        raise TimeoutError(f"Task {task_id} did not complete in {timeout_s}s")

    def queue_depth(self) -> dict:
        return {
            "pending": self._queue.qsize(),
            "running": sum(w.active_tasks for w in self._workers.values()),
            "dlq": len(self._dlq),
        }

    def stats(self) -> dict:
        return {
            **self._stats,
            "workers": {wid: w.to_dict() for wid, w in self._workers.items()},
            "queue": self.queue_depth(),
            "handlers": list(self._handlers.keys()),
        }


async def main():
    import sys

    dispatcher = TaskDispatcherV2()
    await dispatcher.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        results_log = []

        async def llm_handler(task: Task) -> str:
            await asyncio.sleep(0.1 + task.payload.get("complexity", 1) * 0.05)
            return f"LLM result for: {task.payload.get('prompt', '')[:30]}"

        async def embed_handler(task: Task) -> list:
            await asyncio.sleep(0.02)
            return [0.1, 0.2, 0.3]

        dispatcher.register_handler("llm", llm_handler)
        dispatcher.register_handler("embed", embed_handler)
        dispatcher.add_worker("w1", task_types=["llm", "embed"], concurrency=3)
        await dispatcher.start()

        tasks = await dispatcher.submit_many(
            [
                {
                    "task_type": "llm",
                    "payload": {"prompt": "What is Redis?", "complexity": 2},
                    "priority": Priority.NORMAL,
                },
                {
                    "task_type": "llm",
                    "payload": {"prompt": "Explain GPUs", "complexity": 3},
                    "priority": Priority.HIGH,
                },
                {
                    "task_type": "embed",
                    "payload": {"text": "hello world"},
                    "priority": Priority.LOW,
                },
                {
                    "task_type": "llm",
                    "payload": {"prompt": "Critical system query", "complexity": 1},
                    "priority": Priority.CRITICAL,
                },
            ]
        )

        for task in tasks:
            try:
                done = await dispatcher.wait(task.task_id, timeout_s=10.0)
                print(
                    f"  [{done.priority.name:<8}] {done.task_type} [{done.task_id}]: {done.status.value} in {done.duration_ms:.0f}ms"
                )
            except TimeoutError:
                print(f"  [{task.task_id}] TIMEOUT")

        print(
            f"\nStats: {json.dumps({k: v for k, v in dispatcher.stats().items() if k != 'workers'}, indent=2)}"
        )
        dispatcher.stop()

    elif cmd == "stats":
        print(json.dumps(dispatcher.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
