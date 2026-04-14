#!/usr/bin/env python3
"""
jarvis_saga_orchestrator — Distributed saga pattern for multi-step transactions
Choreography + orchestration modes, compensation on failure, step retry
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.saga_orchestrator")

REDIS_PREFIX = "jarvis:saga:"


class SagaState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    COMPENSATING = "compensating"
    COMPENSATED = "compensated"
    PARTIAL = "partial"


class StepState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    COMPENSATED = "compensated"


@dataclass
class SagaStep:
    name: str
    action: Callable
    compensate: Callable | None = None
    retry_max: int = 2
    timeout_s: float = 30.0
    skip_on_fail: bool = False  # if True, saga continues even if this step fails


@dataclass
class StepRecord:
    name: str
    state: StepState = StepState.PENDING
    result: Any = None
    error: str = ""
    attempts: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "state": self.state.value,
            "result": self.result,
            "error": self.error,
            "attempts": self.attempts,
            "duration_ms": round((self.finished_at - self.started_at) * 1000, 2)
            if self.finished_at
            else 0,
        }


@dataclass
class SagaRecord:
    saga_id: str
    name: str
    context: dict[str, Any]
    state: SagaState = SagaState.PENDING
    steps: list[StepRecord] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "saga_id": self.saga_id,
            "name": self.name,
            "state": self.state.value,
            "steps": [s.to_dict() for s in self.steps],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": round((self.finished_at - self.started_at) * 1000, 2)
            if self.finished_at
            else 0,
            "error": self.error,
        }


@dataclass
class SagaDefinition:
    name: str
    steps: list[SagaStep]
    description: str = ""


class SagaOrchestrator:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._definitions: dict[str, SagaDefinition] = {}
        self._records: dict[str, SagaRecord] = {}
        self._max_records = 500
        self._stats: dict[str, int] = {
            "started": 0,
            "success": 0,
            "failed": 0,
            "compensated": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def define(self, definition: SagaDefinition):
        self._definitions[definition.name] = definition

    async def run(
        self,
        saga_name: str,
        context: dict | None = None,
    ) -> SagaRecord:
        import secrets

        definition = self._definitions.get(saga_name)
        if not definition:
            raise ValueError(f"Unknown saga: {saga_name!r}")

        saga_id = secrets.token_hex(8)
        context = dict(context or {})
        record = SagaRecord(
            saga_id=saga_id,
            name=saga_name,
            context=context,
            state=SagaState.RUNNING,
            steps=[StepRecord(name=s.name) for s in definition.steps],
        )
        self._records[saga_id] = record
        if len(self._records) > self._max_records:
            oldest = next(iter(self._records))
            del self._records[oldest]

        self._stats["started"] += 1
        log.info(
            f"Saga {saga_name!r} [{saga_id}] started, {len(definition.steps)} steps"
        )

        executed: list[tuple[SagaStep, StepRecord]] = []

        for i, step_def in enumerate(definition.steps):
            step_rec = record.steps[i]
            step_rec.state = StepState.RUNNING
            step_rec.started_at = time.time()

            success = False
            for attempt in range(1, step_def.retry_max + 2):
                step_rec.attempts = attempt
                try:
                    coro = step_def.action(context)
                    if asyncio.iscoroutine(coro):
                        result = await asyncio.wait_for(
                            coro, timeout=step_def.timeout_s
                        )
                    else:
                        result = coro

                    step_rec.result = result
                    step_rec.state = StepState.SUCCESS
                    step_rec.finished_at = time.time()
                    # Merge result into context
                    if isinstance(result, dict):
                        context.update(result)
                    executed.append((step_def, step_rec))
                    log.debug(f"  Step {step_def.name!r} OK (attempt {attempt})")
                    success = True
                    break

                except Exception as e:
                    step_rec.error = str(e)
                    log.warning(
                        f"  Step {step_def.name!r} attempt {attempt} failed: {e}"
                    )
                    if attempt < step_def.retry_max + 1:
                        await asyncio.sleep(0.1 * attempt)

            if not success:
                step_rec.state = StepState.FAILED
                step_rec.finished_at = time.time()

                if step_def.skip_on_fail:
                    step_rec.state = StepState.SKIPPED
                    log.info(f"  Step {step_def.name!r} skipped (skip_on_fail=True)")
                    continue

                # Trigger compensation
                record.state = SagaState.COMPENSATING
                record.error = f"step {step_def.name!r} failed: {step_rec.error}"
                await self._compensate(executed)
                record.state = SagaState.COMPENSATED
                record.finished_at = time.time()
                self._stats["failed"] += 1
                self._stats["compensated"] += 1
                if self.redis:
                    asyncio.create_task(self._redis_store(record))
                return record

        record.state = SagaState.SUCCESS
        record.finished_at = time.time()
        self._stats["success"] += 1
        log.info(f"Saga {saga_name!r} [{saga_id}] completed successfully")
        if self.redis:
            asyncio.create_task(self._redis_store(record))
        return record

    async def _compensate(self, executed: list[tuple[SagaStep, StepRecord]]):
        """Run compensating actions in reverse order."""
        for step_def, step_rec in reversed(executed):
            if step_def.compensate is None:
                continue
            try:
                coro = step_def.compensate(step_rec.result)
                if asyncio.iscoroutine(coro):
                    await coro
                step_rec.state = StepState.COMPENSATED
                log.info(f"  Compensated step {step_def.name!r}")
            except Exception as e:
                log.error(f"  Compensation for {step_def.name!r} failed: {e}")

    async def _redis_store(self, record: SagaRecord):
        if not self.redis:
            return
        try:
            await self.redis.setex(
                f"{REDIS_PREFIX}{record.saga_id}",
                3600,
                json.dumps(record.to_dict()),
            )
        except Exception:
            pass

    def get(self, saga_id: str) -> SagaRecord | None:
        return self._records.get(saga_id)

    def recent(self, limit: int = 10) -> list[dict]:
        recs = list(self._records.values())
        return [r.to_dict() for r in recs[-limit:]]

    def stats(self) -> dict:
        return {
            **self._stats,
            "active": sum(
                1 for r in self._records.values() if r.state == SagaState.RUNNING
            ),
            "definitions": len(self._definitions),
        }


def build_jarvis_saga_orchestrator() -> SagaOrchestrator:
    orch = SagaOrchestrator()

    # Saga: model deployment (load → verify → register)
    async def step_load_model(ctx: dict) -> dict:
        await asyncio.sleep(0.01)
        return {"loaded": True, "model": ctx.get("model", "unknown")}

    async def step_verify_model(ctx: dict) -> dict:
        if not ctx.get("loaded"):
            raise RuntimeError("model not loaded")
        return {"verified": True}

    async def step_register_model(ctx: dict) -> dict:
        return {"registered": ctx.get("model"), "ts": time.time()}

    async def compensate_load(result: Any):
        log.info(f"Unloading model after failure: {result}")

    orch.define(
        SagaDefinition(
            name="model.deploy",
            description="Load, verify, and register a model on a node",
            steps=[
                SagaStep("load_model", step_load_model, compensate_load, retry_max=2),
                SagaStep("verify_model", step_verify_model, retry_max=1),
                SagaStep("register_model", step_register_model, retry_max=2),
            ],
        )
    )

    # Saga: budget allocation (reserve → notify → commit)
    async def step_reserve_budget(ctx: dict) -> dict:
        return {"reserved": ctx.get("amount", 0), "reservation_id": "res-001"}

    async def step_notify_budget(ctx: dict) -> dict:
        return {"notified": True}

    async def step_commit_budget(ctx: dict) -> dict:
        return {"committed": True, "reservation_id": ctx.get("reservation_id")}

    async def compensate_reserve(result: Any):
        log.info(f"Releasing budget reservation: {result}")

    orch.define(
        SagaDefinition(
            name="budget.allocate",
            description="Reserve, notify, and commit a budget allocation",
            steps=[
                SagaStep("reserve_budget", step_reserve_budget, compensate_reserve),
                SagaStep("notify_budget", step_notify_budget, skip_on_fail=True),
                SagaStep("commit_budget", step_commit_budget),
            ],
        )
    )

    return orch


async def main():
    import sys

    orch = build_jarvis_saga_orchestrator()
    await orch.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Saga orchestrator demo...")

        r1 = await orch.run("model.deploy", {"model": "qwen3.5-9b", "node": "m1"})
        icon = "✅" if r1.state == SagaState.SUCCESS else "❌"
        print(f"\n  {icon} model.deploy → {r1.state.value}")
        for s in r1.steps:
            print(f"    {s.name:<20} {s.state.value}")

        r2 = await orch.run("budget.allocate", {"amount": 5.0})
        icon = "✅" if r2.state == SagaState.SUCCESS else "❌"
        print(f"\n  {icon} budget.allocate → {r2.state.value}")
        for s in r2.steps:
            print(f"    {s.name:<20} {s.state.value}")

        print(f"\nStats: {json.dumps(orch.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(orch.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
