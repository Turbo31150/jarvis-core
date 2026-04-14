#!/usr/bin/env python3
"""
jarvis_task_executor — Autonomous task execution with LLM planning + tool dispatch
Breaks complex tasks into steps, executes via available tools, reports results
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.task_executor")

REDIS_PREFIX = "jarvis:task_exec:"
LM_URL = "http://127.0.0.1:1234"
PLANNER_MODEL = "qwen/qwen3.5-9b"

SYSTEM_PROMPT = """Tu es un assistant d'orchestration JARVIS.
Quand tu reçois une tâche, génère un plan d'exécution JSON avec cette structure:
{
  "steps": [
    {"id": 1, "action": "bash|http_get|llm_query|redis_set|redis_get", "args": {...}, "description": "..."},
    ...
  ],
  "expected_outcome": "..."
}
Actions disponibles:
- bash: {"command": "cmd string"} — exécute une commande shell
- http_get: {"url": "...", "timeout": 5} — GET HTTP
- llm_query: {"prompt": "...", "model": "...", "max_tokens": 200}
- redis_set: {"key": "...", "value": "...", "ex": 3600}
- redis_get: {"key": "..."}
Réponds UNIQUEMENT avec le JSON, pas de texte autour."""


@dataclass
class TaskStep:
    step_id: int
    action: str
    args: dict
    description: str
    status: str = "pending"  # pending | running | ok | error
    result: Any = None
    error: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0

    @property
    def duration_s(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.ended_at or time.time()
        return round(end - self.started_at, 3)


@dataclass
class TaskExecution:
    task_id: str
    goal: str
    steps: list[TaskStep] = field(default_factory=list)
    status: str = "planning"  # planning | running | ok | error
    started_at: float = field(default_factory=time.time)
    ended_at: float = 0.0
    summary: str = ""

    @property
    def duration_s(self) -> float:
        end = self.ended_at or time.time()
        return round(end - self.started_at, 3)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "status": self.status,
            "duration_s": self.duration_s,
            "summary": self.summary,
            "steps": [
                {
                    "id": s.step_id,
                    "action": s.action,
                    "description": s.description,
                    "status": s.status,
                    "duration_s": s.duration_s,
                    "result": str(s.result)[:200] if s.result else None,
                    "error": s.error[:100] if s.error else None,
                }
                for s in self.steps
            ],
        }


class TaskExecutor:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._executions: dict[str, TaskExecution] = {}

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    async def _plan(self, goal: str) -> list[dict]:
        """Use LLM to generate execution plan."""
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60)
        ) as sess:
            async with sess.post(
                f"{LM_URL}/v1/chat/completions",
                json={
                    "model": PLANNER_MODEL,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": f"Tâche: {goal}"},
                    ],
                    "max_tokens": 1000,
                    "temperature": 0,
                },
            ) as r:
                data = await r.json()

        content = data["choices"][0]["message"]["content"]

        # Extract JSON from response
        import re

        json_match = re.search(r"\{.*\}", content, re.DOTALL)
        if not json_match:
            return []
        plan = json.loads(json_match.group())
        return plan.get("steps", [])

    async def _execute_step(self, step: TaskStep) -> Any:
        """Execute a single task step."""
        step.status = "running"
        step.started_at = time.time()

        try:
            if step.action == "bash":
                import subprocess

                r = subprocess.run(
                    step.args["command"],
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                result = r.stdout.strip() or r.stderr.strip()

            elif step.action == "http_get":
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=step.args.get("timeout", 5))
                ) as sess:
                    async with sess.get(step.args["url"]) as r:
                        result = await r.text()
                        result = result[:500]

            elif step.action == "llm_query":
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=60)
                ) as sess:
                    async with sess.post(
                        f"{LM_URL}/v1/chat/completions",
                        json={
                            "model": step.args.get("model", PLANNER_MODEL),
                            "messages": [
                                {"role": "user", "content": step.args["prompt"]}
                            ],
                            "max_tokens": step.args.get("max_tokens", 200),
                            "temperature": 0,
                        },
                    ) as r:
                        data = await r.json()
                result = data["choices"][0]["message"]["content"]

            elif step.action == "redis_set":
                if self.redis:
                    await self.redis.set(
                        step.args["key"],
                        step.args["value"],
                        ex=step.args.get("ex", 3600),
                    )
                    result = "OK"
                else:
                    result = "Redis unavailable"

            elif step.action == "redis_get":
                if self.redis:
                    result = await self.redis.get(step.args["key"])
                else:
                    result = None

            else:
                result = f"Unknown action: {step.action}"

            step.result = result
            step.status = "ok"
            step.ended_at = time.time()
            return result

        except Exception as e:
            step.error = str(e)
            step.status = "error"
            step.ended_at = time.time()
            log.error(f"Step {step.step_id} ({step.action}) failed: {e}")
            return None

    async def execute(self, goal: str, auto_plan: bool = True) -> TaskExecution:
        task_id = str(uuid.uuid4())[:8]
        execution = TaskExecution(task_id=task_id, goal=goal)
        self._executions[task_id] = execution

        log.info(f"Task {task_id}: {goal[:60]}")

        try:
            if auto_plan:
                steps_raw = await self._plan(goal)
                if not steps_raw:
                    execution.status = "error"
                    execution.summary = "Planning failed — no steps generated"
                    return execution

                execution.steps = [
                    TaskStep(
                        step_id=s.get("id", i + 1),
                        action=s.get("action", "bash"),
                        args=s.get("args", {}),
                        description=s.get("description", ""),
                    )
                    for i, s in enumerate(steps_raw)
                ]

            execution.status = "running"

            for step in execution.steps:
                log.info(f"  Step {step.step_id}: {step.description} [{step.action}]")
                result = await self._execute_step(step)
                if step.status == "error":
                    # Non-critical: continue anyway
                    log.warning(f"  Step {step.step_id} errored, continuing")

            ok_count = sum(1 for s in execution.steps if s.status == "ok")
            execution.status = "ok" if ok_count == len(execution.steps) else "partial"
            execution.ended_at = time.time()
            execution.summary = f"{ok_count}/{len(execution.steps)} steps OK in {execution.duration_s:.2f}s"

        except Exception as e:
            execution.status = "error"
            execution.summary = str(e)
            execution.ended_at = time.time()
            log.error(f"Task {task_id} failed: {e}")

        if self.redis:
            await self.redis.set(
                f"{REDIS_PREFIX}{task_id}",
                json.dumps(execution.to_dict()),
                ex=3600,
            )

        return execution

    def get(self, task_id: str) -> TaskExecution | None:
        return self._executions.get(task_id)

    def list_recent(self, limit: int = 10) -> list[dict]:
        return [
            e.to_dict()
            for e in sorted(
                self._executions.values(),
                key=lambda x: -x.started_at,
            )[:limit]
        ]


# ── CLI ───────────────────────────────────────────────────────────────────────


async def main():
    import sys

    executor = TaskExecutor()
    await executor.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"

    if cmd == "run" and len(sys.argv) > 2:
        goal = " ".join(sys.argv[2:])
        print(f"Executing: {goal}")
        exec_ = await executor.execute(goal)
        print(f"\nStatus: {exec_.status} | {exec_.summary}")
        for s in exec_.steps:
            icon = "✅" if s.status == "ok" else "❌"
            print(f"  {icon} [{s.action}] {s.description}")
            if s.result:
                print(f"       → {str(s.result)[:100]}")
            if s.error:
                print(f"       ❗ {s.error[:80]}")

    elif cmd == "list":
        recents = executor.list_recent()
        if not recents:
            print("No executions")
        else:
            for e in recents:
                print(
                    f"  [{e['task_id']}] {e['status']} — {e['goal'][:50]} ({e['duration_s']:.1f}s)"
                )

    else:
        print("Usage: jarvis_task_executor.py run <goal>")
        print("       jarvis_task_executor.py list")


if __name__ == "__main__":
    asyncio.run(main())
