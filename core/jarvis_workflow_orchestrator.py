#!/usr/bin/env python3
"""
jarvis_workflow_orchestrator — DAG-based workflow execution with parallel steps and rollback
Orchestrates multi-step workflows with dependency resolution, retry, and state persistence
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

log = logging.getLogger("jarvis.workflow_orchestrator")

REDIS_PREFIX = "jarvis:workflow:"


class StepState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class WorkflowState(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class WorkflowStep:
    id: str
    name: str
    fn: Callable
    deps: list[str] = field(default_factory=list)
    timeout_s: float = 60.0
    retries: int = 0
    optional: bool = False
    rollback_fn: Callable | None = None


@dataclass
class StepRecord:
    id: str
    name: str
    state: StepState = StepState.PENDING
    output: Any = None
    error: str = ""
    attempts: int = 0
    latency_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "state": self.state.value,
            "error": self.error,
            "attempts": self.attempts,
            "latency_ms": round(self.latency_ms, 1),
        }


@dataclass
class WorkflowRun:
    run_id: str
    workflow: str
    state: WorkflowState = WorkflowState.CREATED
    steps: dict[str, StepRecord] = field(default_factory=dict)
    ctx: dict = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    ended_at: float = 0.0
    error: str = ""

    @property
    def elapsed_ms(self) -> float:
        end = self.ended_at if self.ended_at else time.time()
        return (end - self.started_at) * 1000

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "workflow": self.workflow,
            "state": self.state.value,
            "steps": {k: v.to_dict() for k, v in self.steps.items()},
            "elapsed_ms": round(self.elapsed_ms, 1),
            "error": self.error,
        }


class WorkflowOrchestrator:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._workflows: dict[str, list[WorkflowStep]] = {}
        self._runs: dict[str, WorkflowRun] = {}
        self._stats: dict[str, int] = {"runs": 0, "done": 0, "failed": 0, "retries": 0}

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def define(self, name: str, steps: list[WorkflowStep]):
        self._workflows[name] = steps

    def _waves(self, steps: list[WorkflowStep]) -> list[list[WorkflowStep]]:
        step_map = {s.id: s for s in steps}
        done: set[str] = set()
        remaining = set(step_map)
        waves = []
        while remaining:
            wave = [
                step_map[sid]
                for sid in remaining
                if all(d in done for d in step_map[sid].deps)
            ]
            if not wave:
                raise ValueError("Dependency cycle detected")
            waves.append(wave)
            for s in wave:
                remaining.discard(s.id)
                done.add(s.id)
        return waves

    async def _run_step(self, step: WorkflowStep, run: WorkflowRun) -> StepRecord:
        rec = run.steps.setdefault(step.id, StepRecord(step.id, step.name))
        rec.state = StepState.RUNNING
        t0 = time.time()
        last_err = ""
        for attempt in range(1, step.retries + 2):
            rec.attempts = attempt
            try:
                coro = (
                    step.fn(run.ctx)
                    if asyncio.iscoroutinefunction(step.fn)
                    else asyncio.to_thread(step.fn, run.ctx)
                )
                out = await asyncio.wait_for(coro, timeout=step.timeout_s)
                rec.output = out
                rec.state = StepState.DONE
                rec.latency_ms = (time.time() - t0) * 1000
                if isinstance(out, dict):
                    run.ctx.update(out)
                return rec
            except Exception as e:
                last_err = str(e)[:150]
                if attempt <= step.retries:
                    self._stats["retries"] += 1
                    await asyncio.sleep(min(2**attempt, 30))
        rec.state = StepState.FAILED
        rec.error = last_err
        rec.latency_ms = (time.time() - t0) * 1000
        return rec

    async def execute(self, name: str, ctx: dict | None = None) -> WorkflowRun:
        steps = self._workflows.get(name)
        if not steps:
            raise ValueError(f"Workflow '{name}' not defined")

        run = WorkflowRun(
            run_id=str(uuid.uuid4())[:8],
            workflow=name,
            state=WorkflowState.RUNNING,
            ctx=dict(ctx or {}),
        )
        self._runs[run.run_id] = run
        self._stats["runs"] += 1
        aborted = False

        for step in steps:
            run.steps[step.id] = StepRecord(step.id, step.name)

        try:
            for wave in self._waves(steps):
                if aborted:
                    for s in wave:
                        run.steps[s.id].state = StepState.SKIPPED
                    continue
                tasks = [asyncio.create_task(self._run_step(s, run)) for s in wave]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for step, res in zip(wave, results):
                    if isinstance(res, Exception):
                        run.steps[step.id].state = StepState.FAILED
                        run.steps[step.id].error = str(res)
                        res = run.steps[step.id]
                    if res.state == StepState.FAILED and not step.optional:
                        aborted = True
                        run.error = f"Step '{step.name}' failed: {res.error}"
        except Exception as e:
            run.error = str(e)
            aborted = True

        if aborted:
            run.state = WorkflowState.FAILED
            self._stats["failed"] += 1
            # Rollback in reverse
            done_steps = [
                s
                for s in reversed(steps)
                if run.steps.get(s.id, StepRecord("", "", StepState.PENDING)).state
                == StepState.DONE
                and s.rollback_fn
            ]
            for s in done_steps:
                try:
                    await s.rollback_fn(run.ctx) if asyncio.iscoroutinefunction(
                        s.rollback_fn
                    ) else s.rollback_fn(run.ctx)
                except Exception:
                    pass
        else:
            run.state = WorkflowState.DONE
            self._stats["done"] += 1

        run.ended_at = time.time()

        if self.redis:
            asyncio.create_task(
                self.redis.setex(
                    f"{REDIS_PREFIX}{run.run_id}", 3600, json.dumps(run.to_dict())
                )
            )

        return run

    def stats(self) -> dict:
        return {
            **self._stats,
            "workflows": len(self._workflows),
            "runs_stored": len(self._runs),
        }


def build_jarvis_orchestrator() -> WorkflowOrchestrator:
    orch = WorkflowOrchestrator()

    async def validate(ctx):
        await asyncio.sleep(0.005)
        return {"validated": True}

    async def fetch_context(ctx):
        await asyncio.sleep(0.01)
        return {"context_ready": True}

    async def run_rag(ctx):
        await asyncio.sleep(0.02)
        return {"rag_docs": ["doc1", "doc2"]}

    async def build_prompt(ctx):
        await asyncio.sleep(0.005)
        return {"prompt_built": True}

    async def call_llm(ctx):
        await asyncio.sleep(0.05)
        return {"response": "LLM output"}

    async def post_process(ctx):
        await asyncio.sleep(0.005)
        return {"processed": True}

    orch.define(
        "llm_with_rag",
        [
            WorkflowStep("validate", "Validate Input", validate, timeout_s=5),
            WorkflowStep(
                "fetch_ctx", "Fetch Context", fetch_context, deps=["validate"]
            ),
            WorkflowStep("rag", "RAG Retrieval", run_rag, deps=["validate"]),
            WorkflowStep(
                "prompt", "Build Prompt", build_prompt, deps=["fetch_ctx", "rag"]
            ),
            WorkflowStep(
                "llm",
                "LLM Inference",
                call_llm,
                deps=["prompt"],
                timeout_s=60,
                retries=1,
            ),
            WorkflowStep(
                "post", "Post-process", post_process, deps=["llm"], optional=True
            ),
        ],
    )
    return orch


async def main():
    import sys

    orch = build_jarvis_orchestrator()
    await orch.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Running llm_with_rag workflow...")
        run = await orch.execute("llm_with_rag", {"query": "What is JARVIS?"})
        print(f"State: {run.state.value}  Elapsed: {run.elapsed_ms:.0f}ms")
        for sid, rec in run.steps.items():
            icon = {"done": "✅", "failed": "❌", "skipped": "⏭️"}.get(
                rec.state.value, "?"
            )
            print(f"  {icon} {rec.name:<25} {rec.latency_ms:.0f}ms")
        print(f"\nContext keys: {list(run.ctx.keys())}")
        print(f"\nStats: {json.dumps(orch.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(orch.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
