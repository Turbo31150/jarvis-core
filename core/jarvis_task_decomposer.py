#!/usr/bin/env python3
"""
jarvis_task_decomposer — Decompose complex tasks into executable subtask trees
Dependency resolution, parallel grouping, cost estimation, execution ordering
"""

import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

log = logging.getLogger("jarvis.task_decomposer")


class SubtaskStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"  # all deps satisfied
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class SubtaskKind(str, Enum):
    LLM_CALL = "llm_call"
    TOOL_CALL = "tool_call"
    BASH = "bash"
    SEARCH = "search"
    AGGREGATE = "aggregate"
    DECISION = "decision"
    HUMAN_INPUT = "human_input"
    CUSTOM = "custom"


@dataclass
class Subtask:
    task_id: str
    title: str
    kind: SubtaskKind
    description: str = ""
    depends_on: list[str] = field(default_factory=list)  # task_ids
    estimated_tokens: int = 0
    estimated_ms: int = 0
    priority: int = 50
    status: SubtaskStatus = SubtaskStatus.PENDING
    result: Any = None
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        if self.started_at and self.finished_at:
            return (self.finished_at - self.started_at) * 1000
        return 0.0

    def mark_ready(self):
        self.status = SubtaskStatus.READY

    def mark_running(self):
        self.status = SubtaskStatus.RUNNING
        self.started_at = time.time()

    def mark_done(self, result: Any = None):
        self.status = SubtaskStatus.DONE
        self.result = result
        self.finished_at = time.time()

    def mark_failed(self, error: str):
        self.status = SubtaskStatus.FAILED
        self.error = error
        self.finished_at = time.time()

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "kind": self.kind.value,
            "status": self.status.value,
            "depends_on": self.depends_on,
            "estimated_tokens": self.estimated_tokens,
            "estimated_ms": self.estimated_ms,
            "priority": self.priority,
            "duration_ms": round(self.duration_ms, 1),
            "error": self.error,
        }


@dataclass
class DecompositionPlan:
    plan_id: str
    goal: str
    subtasks: list[Subtask]
    created_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def get(self, task_id: str) -> Subtask | None:
        return next((t for t in self.subtasks if t.task_id == task_id), None)

    def ready_tasks(self) -> list[Subtask]:
        done_ids = {t.task_id for t in self.subtasks if t.status == SubtaskStatus.DONE}
        result = []
        for t in self.subtasks:
            if t.status != SubtaskStatus.PENDING:
                continue
            if all(dep in done_ids for dep in t.depends_on):
                result.append(t)
        return sorted(result, key=lambda t: -t.priority)

    def parallel_groups(self) -> list[list[Subtask]]:
        """Return execution waves: tasks that can run in parallel."""
        done_ids: set[str] = set()
        remaining = list(self.subtasks)
        groups: list[list[Subtask]] = []

        while remaining:
            wave = [
                t for t in remaining if all(dep in done_ids for dep in t.depends_on)
            ]
            if not wave:
                break  # circular dependency or stuck
            groups.append(sorted(wave, key=lambda t: -t.priority))
            wave_ids = {t.task_id for t in wave}
            done_ids |= wave_ids
            remaining = [t for t in remaining if t.task_id not in wave_ids]

        return groups

    def is_complete(self) -> bool:
        return all(
            t.status in (SubtaskStatus.DONE, SubtaskStatus.SKIPPED)
            for t in self.subtasks
        )

    def has_failures(self) -> bool:
        return any(t.status == SubtaskStatus.FAILED for t in self.subtasks)

    def total_estimated_tokens(self) -> int:
        return sum(t.estimated_tokens for t in self.subtasks)

    def total_estimated_ms(self) -> int:
        """Critical path duration (max across parallel groups)."""
        total = 0
        for group in self.parallel_groups():
            total += max((t.estimated_ms for t in group), default=0)
        return total

    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "goal": self.goal[:100],
            "subtask_count": len(self.subtasks),
            "total_estimated_tokens": self.total_estimated_tokens(),
            "critical_path_ms": self.total_estimated_ms(),
            "parallel_groups": len(self.parallel_groups()),
            "subtasks": [t.to_dict() for t in self.subtasks],
        }


class TaskDecomposer:
    """
    Decomposes goals into structured subtask trees using heuristic patterns.
    Provides execution ordering with dependency resolution.
    """

    def __init__(self):
        self._templates: dict[str, list[dict]] = {}
        self._stats: dict[str, int] = {
            "decompositions": 0,
            "subtasks_created": 0,
            "executions": 0,
        }
        self._register_default_templates()

    def _register_default_templates(self):
        self._templates["research"] = [
            {
                "id": "search",
                "title": "Search for information",
                "kind": "search",
                "tokens": 500,
                "ms": 2000,
            },
            {
                "id": "read",
                "title": "Read and extract key facts",
                "kind": "llm_call",
                "tokens": 1000,
                "ms": 3000,
                "deps": ["search"],
            },
            {
                "id": "synthesize",
                "title": "Synthesize findings",
                "kind": "llm_call",
                "tokens": 800,
                "ms": 2500,
                "deps": ["read"],
            },
            {
                "id": "report",
                "title": "Generate final report",
                "kind": "llm_call",
                "tokens": 1200,
                "ms": 4000,
                "deps": ["synthesize"],
            },
        ]
        self._templates["code_review"] = [
            {
                "id": "read_code",
                "title": "Read code files",
                "kind": "tool_call",
                "tokens": 2000,
                "ms": 1000,
            },
            {
                "id": "security",
                "title": "Security analysis",
                "kind": "llm_call",
                "tokens": 1500,
                "ms": 3000,
                "deps": ["read_code"],
            },
            {
                "id": "quality",
                "title": "Code quality analysis",
                "kind": "llm_call",
                "tokens": 1500,
                "ms": 3000,
                "deps": ["read_code"],
            },
            {
                "id": "tests",
                "title": "Test coverage check",
                "kind": "llm_call",
                "tokens": 1000,
                "ms": 2000,
                "deps": ["read_code"],
            },
            {
                "id": "summary",
                "title": "Review summary",
                "kind": "aggregate",
                "tokens": 800,
                "ms": 2000,
                "deps": ["security", "quality", "tests"],
            },
        ]
        self._templates["trading_analysis"] = [
            {
                "id": "price_data",
                "title": "Fetch price data",
                "kind": "tool_call",
                "tokens": 200,
                "ms": 500,
            },
            {
                "id": "indicators",
                "title": "Compute technical indicators",
                "kind": "tool_call",
                "tokens": 300,
                "ms": 800,
                "deps": ["price_data"],
            },
            {
                "id": "sentiment",
                "title": "Market sentiment analysis",
                "kind": "llm_call",
                "tokens": 800,
                "ms": 2000,
            },
            {
                "id": "signal",
                "title": "Generate trading signal",
                "kind": "llm_call",
                "tokens": 600,
                "ms": 2000,
                "deps": ["indicators", "sentiment"],
            },
            {
                "id": "risk",
                "title": "Risk assessment",
                "kind": "llm_call",
                "tokens": 500,
                "ms": 1500,
                "deps": ["signal"],
            },
        ]

    def register_template(self, name: str, steps: list[dict]):
        self._templates[name] = steps

    def from_template(self, goal: str, template_name: str) -> DecompositionPlan:
        import secrets

        template = self._templates.get(template_name)
        if not template:
            raise ValueError(f"Unknown template: {template_name!r}")

        plan_id = secrets.token_hex(8)
        subtasks = []
        for step in template:
            subtasks.append(
                Subtask(
                    task_id=f"{plan_id}:{step['id']}",
                    title=step["title"],
                    kind=SubtaskKind(step.get("kind", "custom")),
                    description=step.get("description", ""),
                    depends_on=[f"{plan_id}:{d}" for d in step.get("deps", [])],
                    estimated_tokens=step.get("tokens", 0),
                    estimated_ms=step.get("ms", 0),
                    priority=step.get("priority", 50),
                )
            )

        self._stats["decompositions"] += 1
        self._stats["subtasks_created"] += len(subtasks)
        return DecompositionPlan(plan_id=plan_id, goal=goal, subtasks=subtasks)

    def decompose(
        self,
        goal: str,
        subtask_specs: list[dict],
    ) -> DecompositionPlan:
        """
        Manual decomposition from specs.
        Each spec: {id, title, kind, description?, tokens?, ms?, deps?, priority?}
        """
        import secrets

        plan_id = secrets.token_hex(8)
        subtasks = []
        for spec in subtask_specs:
            tid = f"{plan_id}:{spec['id']}"
            subtasks.append(
                Subtask(
                    task_id=tid,
                    title=spec.get("title", spec["id"]),
                    kind=SubtaskKind(spec.get("kind", "custom")),
                    description=spec.get("description", ""),
                    depends_on=[f"{plan_id}:{d}" for d in spec.get("deps", [])],
                    estimated_tokens=spec.get("tokens", 0),
                    estimated_ms=spec.get("ms", 0),
                    priority=spec.get("priority", 50),
                    metadata=spec.get("metadata", {}),
                )
            )

        self._stats["decompositions"] += 1
        self._stats["subtasks_created"] += len(subtasks)
        return DecompositionPlan(plan_id=plan_id, goal=goal, subtasks=subtasks)

    def execute_step(
        self,
        plan: DecompositionPlan,
        task_id: str,
        result: Any = None,
        error: str = "",
    ):
        """Mark a subtask as done or failed, trigger ready state propagation."""
        task = plan.get(task_id)
        if not task:
            return
        if error:
            task.mark_failed(error)
        else:
            task.mark_done(result)
        self._stats["executions"] += 1

        # Update ready states
        done_ids = {t.task_id for t in plan.subtasks if t.status == SubtaskStatus.DONE}
        for t in plan.subtasks:
            if t.status == SubtaskStatus.PENDING:
                if all(dep in done_ids for dep in t.depends_on):
                    t.mark_ready()

    def simulate(self, plan: DecompositionPlan) -> dict:
        """Simulate execution and return timing/token estimates."""
        groups = plan.parallel_groups()
        total_tokens = plan.total_estimated_tokens()
        critical_path = plan.total_estimated_ms()

        return {
            "plan_id": plan.plan_id,
            "subtask_count": len(plan.subtasks),
            "parallel_groups": len(groups),
            "critical_path_ms": critical_path,
            "total_tokens": total_tokens,
            "waves": [
                {
                    "wave": i + 1,
                    "tasks": [t.title for t in group],
                    "max_ms": max(t.estimated_ms for t in group),
                    "tokens": sum(t.estimated_tokens for t in group),
                }
                for i, group in enumerate(groups)
            ],
        }

    def stats(self) -> dict:
        return {**self._stats, "templates": len(self._templates)}


def build_jarvis_task_decomposer() -> TaskDecomposer:
    return TaskDecomposer()


def main():
    import sys

    td = build_jarvis_task_decomposer()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Task decomposer demo...\n")

        # Template-based
        plan = td.from_template(
            "Analyze BTC/USDT for trading opportunity", "trading_analysis"
        )
        sim = td.simulate(plan)

        print(f"  Plan: {plan.plan_id}")
        print(f"  Goal: {plan.goal}")
        print(f"  Subtasks: {sim['subtask_count']} in {sim['parallel_groups']} waves")
        print(
            f"  Critical path: {sim['critical_path_ms']}ms | Tokens: {sim['total_tokens']}"
        )
        print()
        for wave in sim["waves"]:
            print(f"  Wave {wave['wave']}: {wave['tasks']} ({wave['max_ms']}ms)")

        # Manual decomposition
        print("\n  Manual decomposition:")
        plan2 = td.decompose(
            "Review and fix authentication bug",
            [
                {
                    "id": "read",
                    "title": "Read auth module",
                    "kind": "tool_call",
                    "tokens": 1500,
                    "ms": 500,
                },
                {
                    "id": "analyze",
                    "title": "Analyze bug",
                    "kind": "llm_call",
                    "tokens": 1000,
                    "ms": 2000,
                    "deps": ["read"],
                },
                {
                    "id": "fix",
                    "title": "Write fix",
                    "kind": "llm_call",
                    "tokens": 800,
                    "ms": 2000,
                    "deps": ["analyze"],
                },
                {
                    "id": "test",
                    "title": "Write tests",
                    "kind": "llm_call",
                    "tokens": 600,
                    "ms": 1500,
                    "deps": ["analyze"],
                },
                {
                    "id": "pr",
                    "title": "Create PR",
                    "kind": "tool_call",
                    "tokens": 200,
                    "ms": 500,
                    "deps": ["fix", "test"],
                },
            ],
        )

        # Simulate execution
        ready = plan2.ready_tasks()
        print(f"  Ready immediately: {[t.title for t in ready]}")
        td.execute_step(plan2, ready[0].task_id, result={"code": "..."})
        ready2 = plan2.ready_tasks()
        print(f"  Ready after step 1: {[t.title for t in ready2]}")

        print(f"\nStats: {json.dumps(td.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(td.stats(), indent=2))


if __name__ == "__main__":
    main()
