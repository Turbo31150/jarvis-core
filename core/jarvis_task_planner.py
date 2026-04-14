#!/usr/bin/env python3
"""
jarvis_task_planner — Goal decomposition into executable task graphs
Breaks high-level goals into subtasks with dependencies, estimates, and priorities
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.task_planner")

REDIS_PREFIX = "jarvis:planner:"
PLANS_FILE = Path("/home/turbo/IA/Core/jarvis/data/task_plans.jsonl")


class TaskStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"  # all deps satisfied
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class TaskType(str, Enum):
    LLM_CALL = "llm_call"
    TOOL_CALL = "tool_call"
    HUMAN_INPUT = "human_input"
    AGGREGATE = "aggregate"
    CONDITION = "condition"
    PARALLEL = "parallel"


@dataclass
class PlanTask:
    task_id: str
    name: str
    task_type: TaskType
    description: str = ""
    deps: list[str] = field(default_factory=list)  # task_ids that must complete first
    config: dict = field(default_factory=dict)
    estimated_ms: float = 500.0
    priority: int = 5  # 1-10, higher = more important
    status: TaskStatus = TaskStatus.PENDING
    result: Any = None
    error: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0

    @property
    def duration_ms(self) -> float:
        if self.started_at and self.ended_at:
            return round((self.ended_at - self.started_at) * 1000, 1)
        return 0.0

    @property
    def ready(self) -> bool:
        return self.status == TaskStatus.READY

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "name": self.name,
            "type": self.task_type.value,
            "description": self.description,
            "deps": self.deps,
            "priority": self.priority,
            "status": self.status.value,
            "estimated_ms": self.estimated_ms,
            "duration_ms": self.duration_ms,
            "error": self.error,
        }


@dataclass
class Plan:
    plan_id: str
    goal: str
    tasks: dict[str, PlanTask] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    completed_at: float = 0.0
    status: str = "active"  # active | done | failed | cancelled

    def add_task(self, task: PlanTask) -> "Plan":
        self.tasks[task.task_id] = task
        return self

    def ready_tasks(self) -> list[PlanTask]:
        """Tasks whose deps are all done."""
        done_ids = {tid for tid, t in self.tasks.items() if t.status == TaskStatus.DONE}
        return [
            t
            for t in self.tasks.values()
            if t.status == TaskStatus.PENDING and all(d in done_ids for d in t.deps)
        ]

    def critical_path(self) -> list[str]:
        """Longest dependency chain (task names)."""
        memo: dict[str, float] = {}

        def longest(tid: str) -> float:
            if tid in memo:
                return memo[tid]
            task = self.tasks[tid]
            if not task.deps:
                memo[tid] = task.estimated_ms
            else:
                memo[tid] = task.estimated_ms + max(
                    longest(d) for d in task.deps if d in self.tasks
                )
            return memo[tid]

        if not self.tasks:
            return []
        last = max(self.tasks.keys(), key=lambda tid: longest(tid))

        # Trace back
        path = []
        current = last
        while current:
            path.append(self.tasks[current].name)
            task = self.tasks[current]
            if not task.deps:
                break
            current = max(task.deps, key=lambda d: longest(d) if d in self.tasks else 0)
        return list(reversed(path))

    @property
    def estimated_total_ms(self) -> float:
        """Sum of critical path."""
        memo: dict[str, float] = {}

        def longest(tid: str) -> float:
            if tid in memo:
                return memo[tid]
            task = self.tasks[tid]
            memo[tid] = task.estimated_ms + max(
                (longest(d) for d in task.deps if d in self.tasks), default=0
            )
            return memo[tid]

        return max((longest(tid) for tid in self.tasks), default=0)

    def to_dict(self) -> dict:
        done = sum(1 for t in self.tasks.values() if t.status == TaskStatus.DONE)
        return {
            "plan_id": self.plan_id,
            "goal": self.goal,
            "status": self.status,
            "task_count": len(self.tasks),
            "done": done,
            "pct": round(done / max(len(self.tasks), 1) * 100, 0),
            "estimated_total_ms": round(self.estimated_total_ms, 0),
            "critical_path": self.critical_path(),
            "tasks": [t.to_dict() for t in self.tasks.values()],
        }


class TaskPlanner:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._plans: dict[str, Plan] = {}
        PLANS_FILE.parent.mkdir(parents=True, exist_ok=True)

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def create_plan(self, goal: str, plan_id: str | None = None) -> Plan:
        pid = plan_id or str(uuid.uuid4())[:8]
        plan = Plan(plan_id=pid, goal=goal)
        self._plans[pid] = plan
        log.info(f"Plan created [{pid}]: {goal}")
        return plan

    def add_task(
        self,
        plan: Plan,
        name: str,
        task_type: TaskType = TaskType.TOOL_CALL,
        deps: list[str] | None = None,
        description: str = "",
        config: dict | None = None,
        estimated_ms: float = 500.0,
        priority: int = 5,
    ) -> PlanTask:
        task = PlanTask(
            task_id=str(uuid.uuid4())[:6],
            name=name,
            task_type=task_type,
            description=description,
            deps=deps or [],
            config=config or {},
            estimated_ms=estimated_ms,
            priority=priority,
        )
        plan.add_task(task)
        return task

    def mark_done(self, plan: Plan, task_id: str, result: Any = None):
        task = plan.tasks.get(task_id)
        if task:
            task.status = TaskStatus.DONE
            task.result = result
            task.ended_at = time.time()
            # Advance ready tasks
            for t in plan.ready_tasks():
                t.status = TaskStatus.READY

    def mark_failed(self, plan: Plan, task_id: str, error: str = ""):
        task = plan.tasks.get(task_id)
        if task:
            task.status = TaskStatus.FAILED
            task.error = error
            task.ended_at = time.time()

    async def simulate(self, plan: Plan) -> dict:
        """Simulate execution order respecting dependencies."""
        execution_order = []
        pending = {tid: t for tid, t in plan.tasks.items()}
        done: set[str] = set()
        wave = 0

        while pending:
            wave += 1
            ready = [
                tid for tid, t in pending.items() if all(d in done for d in t.deps)
            ]
            if not ready:
                remaining = list(pending.keys())
                return {"error": f"Deadlock detected. Remaining: {remaining}"}

            # Sort by priority desc
            ready.sort(key=lambda tid: -pending[tid].priority)
            execution_order.append(
                {"wave": wave, "tasks": [pending[tid].name for tid in ready]}
            )
            for tid in ready:
                done.add(tid)
                del pending[tid]

        return {
            "waves": execution_order,
            "total_waves": wave,
            "estimated_ms": round(plan.estimated_total_ms, 0),
            "critical_path": plan.critical_path(),
        }

    def get_plan(self, plan_id: str) -> Plan | None:
        return self._plans.get(plan_id)

    def persist(self, plan: Plan):
        with open(PLANS_FILE, "a") as f:
            f.write(json.dumps(plan.to_dict()) + "\n")

    def stats(self) -> dict:
        return {
            "plans": len(self._plans),
            "active": sum(1 for p in self._plans.values() if p.status == "active"),
            "done": sum(1 for p in self._plans.values() if p.status == "done"),
            "total_tasks": sum(len(p.tasks) for p in self._plans.values()),
        }


async def main():
    import sys

    planner = TaskPlanner()
    await planner.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        plan = planner.create_plan("Answer user query about cluster health")

        t_collect = planner.add_task(
            plan,
            "collect_metrics",
            TaskType.TOOL_CALL,
            description="Gather GPU/RAM/CPU stats",
            estimated_ms=200,
        )
        t_fetch = planner.add_task(
            plan,
            "fetch_logs",
            TaskType.TOOL_CALL,
            description="Get recent error logs",
            estimated_ms=150,
        )
        t_embed = planner.add_task(
            plan,
            "embed_context",
            TaskType.LLM_CALL,
            deps=[t_collect.task_id, t_fetch.task_id],
            description="Embed context for RAG",
            estimated_ms=100,
        )
        t_answer = planner.add_task(
            plan,
            "generate_answer",
            TaskType.LLM_CALL,
            deps=[t_embed.task_id],
            description="LLM inference",
            estimated_ms=800,
            priority=9,
        )
        t_format = planner.add_task(
            plan,
            "format_response",
            TaskType.TOOL_CALL,
            deps=[t_answer.task_id],
            description="Format output",
            estimated_ms=50,
        )

        sim = await planner.simulate(plan)
        print(f"Goal: {plan.goal}")
        print(f"Estimated: {sim['estimated_ms']}ms over {sim['total_waves']} waves")
        print(f"Critical path: {' → '.join(sim['critical_path'])}")
        print("\nExecution waves:")
        for wave in sim["waves"]:
            print(f"  Wave {wave['wave']}: {', '.join(wave['tasks'])}")

        print(f"\nPlan overview: {json.dumps(plan.to_dict(), indent=2)[:600]}...")

    elif cmd == "stats":
        print(json.dumps(planner.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
