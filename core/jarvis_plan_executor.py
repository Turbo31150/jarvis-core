#!/usr/bin/env python3
"""
jarvis_plan_executor — Structured plan execution engine with step tracking
Executes multi-step plans with rollback, checkpointing, and parallel branches
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

log = logging.getLogger("jarvis.plan_executor")

CHECKPOINT_FILE = Path("/home/turbo/IA/Core/jarvis/data/plan_checkpoints.jsonl")
REDIS_PREFIX = "jarvis:plan:"


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    ROLLED_BACK = "rolled_back"


class PlanStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ROLLED_BACK = "rolled_back"


@dataclass
class PlanStep:
    step_id: str
    name: str
    action: Callable  # async callable(ctx: dict) → Any
    rollback: Callable | None = None  # async callable(ctx: dict) to undo
    depends_on: list[str] = field(default_factory=list)
    timeout_s: float = 60.0
    retries: int = 0
    critical: bool = True  # if False, failure won't abort the plan
    status: StepStatus = StepStatus.PENDING
    result: Any = None
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    attempt: int = 0

    @property
    def duration_s(self) -> float:
        if self.started_at and self.finished_at:
            return self.finished_at - self.started_at
        return 0.0

    def to_dict(self) -> dict:
        return {
            "step_id": self.step_id,
            "name": self.name,
            "depends_on": self.depends_on,
            "status": self.status.value,
            "attempt": self.attempt,
            "duration_s": round(self.duration_s, 2),
            "error": self.error,
            "critical": self.critical,
        }


@dataclass
class Plan:
    plan_id: str
    name: str
    steps: list[PlanStep] = field(default_factory=list)
    status: PlanStatus = PlanStatus.CREATED
    context: dict = field(default_factory=dict)  # shared state between steps
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    error: str = ""

    @property
    def duration_s(self) -> float:
        if self.started_at and self.finished_at:
            return self.finished_at - self.started_at
        return 0.0

    @property
    def progress_pct(self) -> float:
        if not self.steps:
            return 0.0
        done = sum(
            1 for s in self.steps if s.status in (StepStatus.DONE, StepStatus.SKIPPED)
        )
        return done / len(self.steps) * 100

    def step_by_id(self, step_id: str) -> PlanStep | None:
        return next((s for s in self.steps if s.step_id == step_id), None)

    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "name": self.name,
            "status": self.status.value,
            "progress_pct": round(self.progress_pct, 1),
            "duration_s": round(self.duration_s, 2),
            "steps": [s.to_dict() for s in self.steps],
            "error": self.error,
        }


class PlanExecutor:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._plans: dict[str, Plan] = {}
        self._stats: dict[str, int] = {
            "plans_run": 0,
            "plans_done": 0,
            "plans_failed": 0,
            "steps_run": 0,
            "steps_failed": 0,
            "rollbacks": 0,
        }
        CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def create_plan(
        self, name: str, steps: list[PlanStep], context: dict | None = None
    ) -> Plan:
        plan = Plan(
            plan_id=str(uuid.uuid4())[:12],
            name=name,
            steps=steps,
            context=context or {},
        )
        self._plans[plan.plan_id] = plan
        return plan

    async def _execute_step(self, step: PlanStep, ctx: dict) -> bool:
        for attempt in range(step.retries + 1):
            step.attempt = attempt + 1
            step.status = StepStatus.RUNNING
            step.started_at = time.time()
            self._stats["steps_run"] += 1
            try:
                result = await asyncio.wait_for(
                    step.action(ctx), timeout=step.timeout_s
                )
                step.result = result
                step.status = StepStatus.DONE
                step.finished_at = time.time()
                log.info(
                    f"  Step '{step.name}' done "
                    f"({step.duration_s:.2f}s, attempt {step.attempt})"
                )
                return True
            except asyncio.TimeoutError:
                step.error = f"Timeout after {step.timeout_s}s"
            except Exception as e:
                step.error = str(e)[:200]
            step.finished_at = time.time()
            if attempt < step.retries:
                log.warning(
                    f"  Step '{step.name}' failed (attempt {attempt + 1}): {step.error} — retrying"
                )
                await asyncio.sleep(1.0 * (attempt + 1))

        step.status = StepStatus.FAILED
        self._stats["steps_failed"] += 1
        log.error(f"  Step '{step.name}' FAILED: {step.error}")
        return False

    async def _rollback(self, plan: Plan, up_to_step: str):
        """Roll back completed steps in reverse order."""
        done_steps = [
            s
            for s in reversed(plan.steps)
            if s.status == StepStatus.DONE and s.rollback
        ]
        for step in done_steps:
            log.info(f"  Rolling back: '{step.name}'")
            try:
                await asyncio.wait_for(step.rollback(plan.context), timeout=30.0)
                step.status = StepStatus.ROLLED_BACK
                self._stats["rollbacks"] += 1
            except Exception as e:
                log.warning(f"  Rollback failed for '{step.name}': {e}")

    async def execute(self, plan: Plan, rollback_on_failure: bool = True) -> PlanStatus:
        plan.status = PlanStatus.RUNNING
        plan.started_at = time.time()
        self._stats["plans_run"] += 1

        log.info(f"Executing plan '{plan.name}' ({len(plan.steps)} steps)")

        completed: set[str] = set()
        failed_critical = False
        sem = asyncio.Semaphore(4)  # max parallel steps

        async def run_step(step: PlanStep):
            nonlocal failed_critical
            # Wait for dependencies
            for dep_id in step.depends_on:
                while dep_id not in completed:
                    if failed_critical:
                        step.status = StepStatus.SKIPPED
                        return
                    await asyncio.sleep(0.05)

            if failed_critical and step.critical:
                step.status = StepStatus.SKIPPED
                completed.add(step.step_id)
                return

            async with sem:
                ok = await self._execute_step(step, plan.context)
                completed.add(step.step_id)
                if not ok and step.critical:
                    failed_critical = True
                    plan.error = f"Step '{step.name}' failed: {step.error}"

        await asyncio.gather(*[run_step(s) for s in plan.steps])

        plan.finished_at = time.time()

        if failed_critical:
            plan.status = PlanStatus.FAILED
            self._stats["plans_failed"] += 1
            if rollback_on_failure:
                log.warning(f"Plan '{plan.name}' failed — rolling back")
                await self._rollback(plan, plan.error)
                plan.status = PlanStatus.ROLLED_BACK
        else:
            plan.status = PlanStatus.DONE
            self._stats["plans_done"] += 1
            log.info(f"Plan '{plan.name}' DONE in {plan.duration_s:.2f}s")

        # Checkpoint
        try:
            with open(CHECKPOINT_FILE, "a") as f:
                f.write(json.dumps(plan.to_dict()) + "\n")
        except Exception:
            pass

        if self.redis:
            asyncio.create_task(
                self.redis.setex(
                    f"{REDIS_PREFIX}{plan.plan_id}",
                    3600,
                    json.dumps(plan.to_dict()),
                )
            )

        return plan.status

    def get_plan(self, plan_id: str) -> Plan | None:
        return self._plans.get(plan_id)

    def list_plans(self) -> list[dict]:
        return [p.to_dict() for p in self._plans.values()]

    def stats(self) -> dict:
        return {**self._stats, "active_plans": len(self._plans)}


def build_jarvis_plan_executor() -> PlanExecutor:
    return PlanExecutor()


async def main():
    import sys

    executor = build_jarvis_plan_executor()
    await executor.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        results_log: list[str] = []

        async def step_check_redis(ctx: dict) -> str:
            await asyncio.sleep(0.05)
            ctx["redis_ok"] = True
            results_log.append("redis OK")
            return "redis_connected"

        async def step_load_model(ctx: dict) -> str:
            await asyncio.sleep(0.1)
            ctx["model"] = "qwen3.5-9b"
            results_log.append("model loaded")
            return "model_loaded"

        async def rollback_model(ctx: dict):
            results_log.append("model unloaded (rollback)")

        async def step_run_warmup(ctx: dict) -> str:
            await asyncio.sleep(0.05)
            if not ctx.get("redis_ok"):
                raise RuntimeError("Redis not ready")
            results_log.append("warmup done")
            return "warmup_ok"

        async def step_start_api(ctx: dict) -> str:
            await asyncio.sleep(0.05)
            results_log.append("API started")
            return "api_running"

        steps = [
            PlanStep("s1", "Check Redis", step_check_redis, critical=True),
            PlanStep(
                "s2",
                "Load Model",
                step_load_model,
                rollback=rollback_model,
                depends_on=["s1"],
                critical=True,
            ),
            PlanStep(
                "s3",
                "Run Warmup",
                step_run_warmup,
                depends_on=["s1", "s2"],
                critical=False,
            ),
            PlanStep(
                "s4", "Start API", step_start_api, depends_on=["s2"], critical=True
            ),
        ]

        plan = executor.create_plan("boot_sequence", steps, {"env": "demo"})
        print(f"Executing plan '{plan.name}'...")
        status = await executor.execute(plan)

        print(f"\nPlan status: {status.value}")
        print(f"Progress: {plan.progress_pct:.0f}%")
        print(f"Duration: {plan.duration_s:.2f}s")
        print("\nStep results:")
        for s in plan.steps:
            icon = {"done": "✅", "failed": "❌", "skipped": "⏭️"}.get(
                s.status.value, "?"
            )
            print(
                f"  {icon} [{s.step_id}] {s.name:<20} "
                f"status={s.status.value:<12} {s.duration_s:.3f}s"
            )

        print(f"\nContext after plan: {plan.context}")
        print(f"Log: {results_log}")
        print(f"\nStats: {json.dumps(executor.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(executor.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
