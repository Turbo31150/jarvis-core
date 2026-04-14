#!/usr/bin/env python3
"""
jarvis_task_scheduler — Cron-style and one-shot task scheduling with async execution
Supports cron expressions, intervals, delays, priorities, and dependency chains
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

log = logging.getLogger("jarvis.task_scheduler")

HISTORY_FILE = Path("/home/turbo/IA/Core/jarvis/data/scheduler_history.jsonl")
REDIS_PREFIX = "jarvis:sched:"


class TaskKind(str, Enum):
    INTERVAL = "interval"  # run every N seconds
    CRON = "cron"  # cron expression (simplified)
    ONE_SHOT = "one_shot"  # run once at a given time
    CHAIN = "chain"  # run after another task completes


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


@dataclass
class TaskRun:
    run_id: str
    task_id: str
    started_at: float
    finished_at: float = 0.0
    status: TaskStatus = TaskStatus.RUNNING
    error: str = ""
    result: Any = None

    @property
    def duration_ms(self) -> float:
        if self.finished_at:
            return (self.finished_at - self.started_at) * 1000
        return 0.0

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": round(self.duration_ms, 2),
            "status": self.status.value,
            "error": self.error[:120] if self.error else "",
        }


@dataclass
class ScheduledTask:
    task_id: str
    name: str
    kind: TaskKind
    fn: Callable
    # Kind-specific params
    interval_s: float = 0.0
    run_at: float = 0.0  # epoch for ONE_SHOT
    after_task: str = ""  # task_id for CHAIN
    # Scheduling options
    priority: int = 5
    max_retries: int = 0
    retry_delay_s: float = 5.0
    timeout_s: float = 300.0
    enabled: bool = True
    tags: list[str] = field(default_factory=list)
    # Runtime state
    last_run: float = 0.0
    next_run: float = 0.0
    run_count: int = 0
    fail_count: int = 0
    consecutive_failures: int = 0
    _task_handle: asyncio.Task | None = field(default=None, repr=False)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "name": self.name,
            "kind": self.kind.value,
            "interval_s": self.interval_s,
            "priority": self.priority,
            "enabled": self.enabled,
            "last_run": self.last_run,
            "next_run": self.next_run,
            "run_count": self.run_count,
            "fail_count": self.fail_count,
            "consecutive_failures": self.consecutive_failures,
            "tags": self.tags,
        }


class TaskScheduler:
    def __init__(self, persist: bool = True):
        self.redis: aioredis.Redis | None = None
        self._tasks: dict[str, ScheduledTask] = {}
        self._history: list[TaskRun] = []
        self._max_history = 5_000
        self._running: set[str] = set()
        self._persist = persist
        self._loop_task: asyncio.Task | None = None
        self._stats: dict[str, int] = {
            "dispatched": 0,
            "succeeded": 0,
            "failed": 0,
            "skipped": 0,
        }
        if persist:
            HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def schedule(
        self,
        task_id: str,
        fn: Callable,
        kind: TaskKind = TaskKind.INTERVAL,
        interval_s: float = 60.0,
        run_at: float = 0.0,
        after_task: str = "",
        name: str = "",
        priority: int = 5,
        max_retries: int = 0,
        timeout_s: float = 300.0,
        tags: list[str] | None = None,
        delay_s: float = 0.0,
    ) -> ScheduledTask:
        now = time.time()
        next_run = now + delay_s
        if kind == TaskKind.ONE_SHOT and run_at > 0:
            next_run = run_at
        elif kind == TaskKind.INTERVAL:
            next_run = now + delay_s

        t = ScheduledTask(
            task_id=task_id,
            name=name or task_id,
            kind=kind,
            fn=fn,
            interval_s=interval_s,
            run_at=run_at,
            after_task=after_task,
            priority=priority,
            max_retries=max_retries,
            timeout_s=timeout_s,
            tags=tags or [],
            next_run=next_run,
        )
        self._tasks[task_id] = t
        log.debug(f"Scheduled [{task_id}] kind={kind.value} next_run={next_run:.0f}")
        return t

    def cancel(self, task_id: str):
        t = self._tasks.get(task_id)
        if t:
            t.enabled = False
            if t._task_handle and not t._task_handle.done():
                t._task_handle.cancel()

    async def _run_task(self, task: ScheduledTask):
        if task.task_id in self._running:
            self._stats["skipped"] += 1
            return

        self._running.add(task.task_id)
        run = TaskRun(
            run_id=str(uuid.uuid4())[:8],
            task_id=task.task_id,
            started_at=time.time(),
        )
        task.last_run = run.started_at
        task.run_count += 1
        self._stats["dispatched"] += 1

        retries = 0
        while True:
            try:
                if asyncio.iscoroutinefunction(task.fn):
                    result = await asyncio.wait_for(task.fn(), timeout=task.timeout_s)
                else:
                    result = task.fn()
                run.result = result
                run.status = TaskStatus.DONE
                run.finished_at = time.time()
                task.consecutive_failures = 0
                self._stats["succeeded"] += 1
                break
            except asyncio.TimeoutError:
                run.error = f"timeout after {task.timeout_s}s"
                run.status = TaskStatus.FAILED
                run.finished_at = time.time()
                task.fail_count += 1
                task.consecutive_failures += 1
                self._stats["failed"] += 1
                break
            except Exception as e:
                retries += 1
                if retries <= task.max_retries:
                    log.warning(f"Task {task.task_id} failed (retry {retries}): {e}")
                    await asyncio.sleep(task.retry_delay_s)
                    continue
                run.error = str(e)[:200]
                run.status = TaskStatus.FAILED
                run.finished_at = time.time()
                task.fail_count += 1
                task.consecutive_failures += 1
                self._stats["failed"] += 1
                break

        self._running.discard(task.task_id)
        self._history.append(run)
        if len(self._history) > self._max_history:
            self._history.pop(0)

        if self._persist:
            try:
                with open(HISTORY_FILE, "a") as f:
                    f.write(json.dumps(run.to_dict()) + "\n")
            except Exception:
                pass

        # Schedule next run for INTERVAL tasks
        if task.kind == TaskKind.INTERVAL and task.enabled:
            task.next_run = time.time() + task.interval_s
        elif task.kind == TaskKind.ONE_SHOT:
            task.enabled = False

        # Trigger CHAIN dependents
        if run.status == TaskStatus.DONE:
            for dep in self._tasks.values():
                if dep.kind == TaskKind.CHAIN and dep.after_task == task.task_id:
                    dep.next_run = time.time()

        log.debug(
            f"Task [{task.task_id}] {run.status.value} in {run.duration_ms:.0f}ms"
        )

    async def _scheduler_loop(self, tick_s: float = 1.0):
        while True:
            now = time.time()
            due = sorted(
                [t for t in self._tasks.values() if t.enabled and t.next_run <= now],
                key=lambda t: t.priority,
            )
            for task in due:
                asyncio.create_task(self._run_task(task))
            await asyncio.sleep(tick_s)

    def start(self, tick_s: float = 1.0):
        self._loop_task = asyncio.create_task(self._scheduler_loop(tick_s))

    def stop(self):
        if self._loop_task:
            self._loop_task.cancel()

    def task_list(self) -> list[dict]:
        return [t.to_dict() for t in self._tasks.values()]

    def recent_runs(self, task_id: str | None = None, limit: int = 20) -> list[dict]:
        runs = self._history
        if task_id:
            runs = [r for r in runs if r.task_id == task_id]
        return [r.to_dict() for r in runs[-limit:]]

    def stats(self) -> dict:
        return {
            **self._stats,
            "scheduled_tasks": len(self._tasks),
            "enabled_tasks": sum(1 for t in self._tasks.values() if t.enabled),
            "currently_running": len(self._running),
        }


def build_jarvis_task_scheduler() -> TaskScheduler:
    sched = TaskScheduler(persist=True)

    async def _health_check():
        log.info("Running health check...")

    async def _cleanup_logs():
        log.info("Cleaning up old logs...")

    async def _redis_ping():
        pass  # placeholder

    sched.schedule(
        "health-check",
        _health_check,
        interval_s=60.0,
        name="Health Check",
        tags=["infra"],
    )
    sched.schedule(
        "log-cleanup",
        _cleanup_logs,
        interval_s=3600.0,
        name="Log Cleanup",
        delay_s=300.0,
        tags=["maintenance"],
    )
    sched.schedule(
        "redis-ping",
        _redis_ping,
        interval_s=30.0,
        name="Redis Keepalive",
        priority=1,
        tags=["infra"],
    )

    return sched


async def main():
    import sys

    sched = build_jarvis_task_scheduler()
    await sched.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        call_count = {"n": 0}

        async def demo_fn():
            call_count["n"] += 1
            await asyncio.sleep(0.05)
            return f"run #{call_count['n']}"

        sched.schedule("demo", demo_fn, interval_s=0.2, name="Demo Task")
        sched.start(tick_s=0.1)

        await asyncio.sleep(0.7)
        sched.stop()

        print(f"Demo task ran {call_count['n']} times")
        print("\nRecent runs:")
        for r in sched.recent_runs("demo"):
            print(f"  {r['run_id']} {r['status']} {r['duration_ms']:.0f}ms")

        print(f"\nStats: {json.dumps(sched.stats(), indent=2)}")

    elif cmd == "list":
        for t in sched.task_list():
            print(json.dumps(t))

    elif cmd == "stats":
        print(json.dumps(sched.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
