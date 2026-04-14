#!/usr/bin/env python3
"""
jarvis_task_dependency — DAG-based task dependency tracker with cycle detection
Manages task prerequisites, resolves execution order, tracks completion state
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.task_dependency")

REDIS_PREFIX = "jarvis:taskdep:"


class TaskState(str, Enum):
    PENDING = "pending"
    READY = "ready"  # all deps satisfied
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"  # dep failed and skip_on_failure=True


@dataclass
class DependencyTask:
    task_id: str
    name: str
    deps: list[str] = field(default_factory=list)  # task_ids this depends on
    state: TaskState = TaskState.PENDING
    priority: int = 5  # higher = run sooner
    skip_on_failure: bool = False  # skip if dep failed
    payload: dict = field(default_factory=dict)
    result: dict | None = None
    error: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def duration_ms(self) -> float:
        if self.finished_at and self.started_at:
            return (self.finished_at - self.started_at) * 1000
        return 0.0

    @property
    def is_terminal(self) -> bool:
        return self.state in (TaskState.DONE, TaskState.FAILED, TaskState.SKIPPED)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "name": self.name,
            "deps": self.deps,
            "state": self.state.value,
            "priority": self.priority,
            "duration_ms": round(self.duration_ms, 1),
            "error": self.error,
        }


class CycleError(Exception):
    pass


class TaskDependencyTracker:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._tasks: dict[str, DependencyTask] = {}
        self._on_ready: list[Callable] = []
        self._stats: dict[str, int] = {
            "registered": 0,
            "completed": 0,
            "failed": 0,
            "skipped": 0,
            "cycles_detected": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def on_ready(self, callback: Callable):
        """Register callback(task) when a task becomes ready to run."""
        self._on_ready.append(callback)

    def register(
        self,
        name: str,
        deps: list[str] | None = None,
        priority: int = 5,
        skip_on_failure: bool = False,
        payload: dict | None = None,
        task_id: str | None = None,
    ) -> DependencyTask:
        tid = task_id or str(uuid.uuid4())[:12]
        task = DependencyTask(
            task_id=tid,
            name=name,
            deps=deps or [],
            priority=priority,
            skip_on_failure=skip_on_failure,
            payload=payload or {},
        )
        self._tasks[tid] = task
        self._stats["registered"] += 1
        self._evaluate_readiness(task)
        return task

    def _has_cycle(self) -> list[str] | None:
        """Kahn's algorithm — return cycle node list or None."""
        in_degree: dict[str, int] = {tid: 0 for tid in self._tasks}
        for task in self._tasks.values():
            for dep in task.deps:
                if dep in in_degree:
                    in_degree[task.task_id] = in_degree.get(task.task_id, 0) + 1

        queue = [tid for tid, deg in in_degree.items() if deg == 0]
        visited = 0
        while queue:
            tid = queue.pop(0)
            visited += 1
            # find tasks that depend on tid
            for task in self._tasks.values():
                if tid in task.deps:
                    in_degree[task.task_id] -= 1
                    if in_degree[task.task_id] == 0:
                        queue.append(task.task_id)

        if visited < len(self._tasks):
            # cycle — find involved nodes
            return [tid for tid, deg in in_degree.items() if deg > 0]
        return None

    def validate(self) -> list[str]:
        """Return list of errors (cycles, missing deps)."""
        errors = []
        all_ids = set(self._tasks.keys())
        for task in self._tasks.values():
            for dep in task.deps:
                if dep not in all_ids:
                    errors.append(
                        f"Task {task.task_id}({task.name}) has unknown dep {dep}"
                    )
        cycle = self._has_cycle()
        if cycle:
            self._stats["cycles_detected"] += 1
            errors.append(f"Cycle detected involving: {cycle}")
        return errors

    def _dep_states(self, task: DependencyTask) -> list[TaskState]:
        return [self._tasks[d].state for d in task.deps if d in self._tasks]

    def _evaluate_readiness(self, task: DependencyTask):
        if task.state != TaskState.PENDING:
            return
        dep_states = self._dep_states(task)
        missing = [d for d in task.deps if d not in self._tasks]
        if missing:
            return  # unknown deps → stay pending

        all_done = all(s == TaskState.DONE for s in dep_states)
        any_failed = any(s == TaskState.FAILED for s in dep_states)

        if any_failed:
            if task.skip_on_failure:
                task.state = TaskState.SKIPPED
                self._stats["skipped"] += 1
                self._propagate(task)
            # else: stay PENDING until manually resolved
        elif all_done:
            task.state = TaskState.READY
            for cb in self._on_ready:
                try:
                    cb(task)
                except Exception:
                    pass

    def _propagate(self, finished_task: DependencyTask):
        """Re-evaluate all tasks that depend on this one."""
        for task in self._tasks.values():
            if finished_task.task_id in task.deps:
                self._evaluate_readiness(task)

    def mark_running(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if not task or task.state != TaskState.READY:
            return False
        task.state = TaskState.RUNNING
        task.started_at = time.time()
        return True

    def mark_done(self, task_id: str, result: dict | None = None) -> bool:
        task = self._tasks.get(task_id)
        if not task:
            return False
        task.state = TaskState.DONE
        task.result = result or {}
        task.finished_at = time.time()
        self._stats["completed"] += 1
        self._propagate(task)
        return True

    def mark_failed(self, task_id: str, error: str = "") -> bool:
        task = self._tasks.get(task_id)
        if not task:
            return False
        task.state = TaskState.FAILED
        task.error = error
        task.finished_at = time.time()
        self._stats["failed"] += 1
        self._propagate(task)
        return True

    def ready_tasks(self) -> list[DependencyTask]:
        tasks = [t for t in self._tasks.values() if t.state == TaskState.READY]
        return sorted(tasks, key=lambda t: -t.priority)

    def topological_order(self) -> list[list[str]]:
        """Return tasks in execution waves (parallel groups)."""
        remaining = {tid: set(t.deps) for tid, t in self._tasks.items()}
        waves = []
        while remaining:
            wave = [tid for tid, deps in remaining.items() if not deps]
            if not wave:
                break  # cycle
            waves.append(sorted(wave, key=lambda tid: -self._tasks[tid].priority))
            for tid in wave:
                del remaining[tid]
            for deps in remaining.values():
                deps -= set(wave)
        return waves

    def get(self, task_id: str) -> DependencyTask | None:
        return self._tasks.get(task_id)

    def list_tasks(self, state: TaskState | None = None) -> list[dict]:
        tasks = list(self._tasks.values())
        if state:
            tasks = [t for t in tasks if t.state == state]
        return [t.to_dict() for t in tasks]

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for t in self._tasks.values():
            counts[t.state.value] = counts.get(t.state.value, 0) + 1
        return {
            **self._stats,
            "total": len(self._tasks),
            "by_state": counts,
        }


def build_jarvis_task_tracker() -> TaskDependencyTracker:
    tracker = TaskDependencyTracker()

    def log_ready(task: DependencyTask):
        log.info(f"Task READY: {task.name} (priority={task.priority})")

    tracker.on_ready(log_ready)
    return tracker


async def main():
    import sys

    tracker = build_jarvis_task_tracker()
    await tracker.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        # Build a pipeline: fetch → parse → [enrich, validate] → store
        t_fetch = tracker.register("fetch_data", priority=10)
        t_parse = tracker.register("parse_data", deps=[t_fetch.task_id], priority=9)
        t_enrich = tracker.register("enrich_data", deps=[t_parse.task_id], priority=8)
        t_validate = tracker.register(
            "validate_data", deps=[t_parse.task_id], priority=8
        )
        t_store = tracker.register(
            "store_data",
            deps=[t_enrich.task_id, t_validate.task_id],
            priority=7,
        )

        errors = tracker.validate()
        print(f"Validation: {errors or 'OK'}")

        waves = tracker.topological_order()
        print("\nExecution waves:")
        for i, wave in enumerate(waves):
            names = [tracker.get(tid).name for tid in wave]
            print(f"  Wave {i + 1}: {names}")

        print(f"\nReady tasks: {[t.name for t in tracker.ready_tasks()]}")

        # Simulate execution
        tracker.mark_running(t_fetch.task_id)
        tracker.mark_done(t_fetch.task_id, result={"rows": 100})
        print(f"\nAfter fetch done — ready: {[t.name for t in tracker.ready_tasks()]}")

        tracker.mark_running(t_parse.task_id)
        tracker.mark_done(t_parse.task_id)
        print(f"After parse done — ready: {[t.name for t in tracker.ready_tasks()]}")

        print(f"\nSummary: {json.dumps(tracker.summary(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(tracker.summary(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
