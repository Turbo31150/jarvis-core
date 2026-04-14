#!/usr/bin/env python3
"""
jarvis_agent_planner — Goal-driven agent planning with ReAct loop
Thought/action/observation cycle, tool selection, plan revision, goal tracking
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

log = logging.getLogger("jarvis.agent_planner")


class StepType(str, Enum):
    THOUGHT = "thought"
    ACTION = "action"
    OBSERVATION = "observation"
    REFLECTION = "reflection"
    FINAL_ANSWER = "final_answer"


class PlanStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STALLED = "stalled"
    MAX_STEPS = "max_steps"


class ActionStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    NOT_FOUND = "not_found"


@dataclass
class Step:
    step_id: int
    step_type: StepType
    content: str
    tool_name: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    tool_result: Any = None
    action_status: ActionStatus | None = None
    ts: float = field(default_factory=time.time)
    duration_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "step_id": self.step_id,
            "type": self.step_type.value,
            "content": self.content[:200],
            "tool": self.tool_name,
            "tool_args": self.tool_args,
            "status": self.action_status.value if self.action_status else None,
            "duration_ms": round(self.duration_ms, 2),
        }


@dataclass
class Goal:
    goal_id: str
    description: str
    success_criteria: list[str] = field(default_factory=list)
    priority: int = 50
    max_steps: int = 20
    timeout_s: float = 120.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentPlan:
    plan_id: str
    goal: Goal
    steps: list[Step] = field(default_factory=list)
    status: PlanStatus = PlanStatus.RUNNING
    final_answer: str = ""
    created_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    error: str = ""

    @property
    def step_count(self) -> int:
        return len(self.steps)

    @property
    def duration_ms(self) -> float:
        if self.finished_at:
            return (self.finished_at - self.created_at) * 1000
        return (time.time() - self.created_at) * 1000

    @property
    def action_steps(self) -> list[Step]:
        return [s for s in self.steps if s.step_type == StepType.ACTION]

    def last_observation(self) -> str:
        for s in reversed(self.steps):
            if s.step_type == StepType.OBSERVATION:
                return s.content
        return ""

    def trajectory(self) -> list[dict]:
        return [s.to_dict() for s in self.steps]

    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "goal": self.goal.description[:100],
            "status": self.status.value,
            "step_count": self.step_count,
            "actions_taken": len(self.action_steps),
            "duration_ms": round(self.duration_ms, 1),
            "final_answer": self.final_answer[:200],
            "error": self.error,
        }


# --- Tool registry ---


@dataclass
class AgentTool:
    name: str
    description: str
    fn: Callable
    param_desc: str = ""


# --- Prompt construction helpers ---


def _build_react_prompt(goal: Goal, steps: list[Step], tools: list[AgentTool]) -> str:
    tool_list = "\n".join(
        f"- {t.name}: {t.description}"
        + (f" | args: {t.param_desc}" if t.param_desc else "")
        for t in tools
    )

    history = ""
    for s in steps[-10:]:  # last 10 steps for context
        if s.step_type == StepType.THOUGHT:
            history += f"Thought: {s.content}\n"
        elif s.step_type == StepType.ACTION:
            args_str = json.dumps(s.tool_args) if s.tool_args else ""
            history += f"Action: {s.tool_name}({args_str})\n"
        elif s.step_type == StepType.OBSERVATION:
            history += f"Observation: {s.content}\n"
        elif s.step_type == StepType.REFLECTION:
            history += f"Reflection: {s.content}\n"

    return (
        f"Goal: {goal.description}\n\n"
        f"Available tools:\n{tool_list}\n\n"
        f"{history}\n"
        f"What is your next thought or action? "
        f"Reply with one of:\n"
        f"  Thought: <reasoning>\n"
        f"  Action: <tool_name> {{'arg': 'value'}}\n"
        f"  Final Answer: <answer>\n"
    )


def _parse_react_response(text: str) -> tuple[StepType, str, str, dict]:
    """Parse ReAct response. Returns (step_type, content, tool_name, tool_args)."""
    import re

    text = text.strip()

    if text.lower().startswith("final answer:"):
        return StepType.FINAL_ANSWER, text[13:].strip(), "", {}

    if text.lower().startswith("thought:"):
        return StepType.THOUGHT, text[8:].strip(), "", {}

    if text.lower().startswith("action:"):
        action_text = text[7:].strip()
        # Parse: tool_name {"arg": "val"} or tool_name({"arg": "val"})
        m = re.match(r"(\w[\w._-]*)\s*[\(\{]?(.*?)[\)\}]?\s*$", action_text, re.DOTALL)
        if m:
            tool_name = m.group(1)
            args_raw = m.group(2).strip()
            try:
                args = json.loads("{" + args_raw + "}") if args_raw else {}
            except Exception:
                try:
                    args = json.loads(args_raw) if args_raw else {}
                except Exception:
                    args = {"input": args_raw}
            return StepType.ACTION, action_text, tool_name, args
        return StepType.ACTION, action_text, action_text.split()[0], {}

    # Default to thought
    return StepType.THOUGHT, text, "", {}


class AgentPlanner:
    def __init__(
        self,
        llm_fn: Callable[[str], str] | None = None,
        max_steps: int = 20,
        timeout_s: float = 120.0,
    ):
        self._tools: dict[str, AgentTool] = {}
        self._llm_fn = llm_fn or self._mock_llm
        self._max_steps = max_steps
        self._timeout = timeout_s
        self._plans: list[AgentPlan] = []
        self._stats: dict[str, int] = {
            "plans_created": 0,
            "plans_succeeded": 0,
            "plans_failed": 0,
            "total_steps": 0,
            "tool_calls": 0,
        }

    def register_tool(self, tool: AgentTool):
        self._tools[tool.name] = tool

    def tool(self, name: str, description: str, param_desc: str = ""):
        """Decorator to register a tool function."""

        def decorator(fn: Callable) -> Callable:
            self.register_tool(AgentTool(name, description, fn, param_desc))
            return fn

        return decorator

    def _mock_llm(self, prompt: str) -> str:
        """Minimal mock LLM for testing the loop."""
        if (
            "cluster_health" not in prompt
            and len([s for s in prompt.split("\n") if s.startswith("Observation:")]) < 1
        ):
            return 'Action: cluster_status {"node": "all"}'
        return "Final Answer: The cluster is healthy based on the status check."

    async def _call_llm(self, prompt: str) -> str:
        if asyncio.iscoroutinefunction(self._llm_fn):
            return await self._llm_fn(prompt)
        return self._llm_fn(prompt)

    async def _execute_tool(
        self, tool_name: str, args: dict
    ) -> tuple[Any, ActionStatus]:
        tool = self._tools.get(tool_name)
        if not tool:
            return f"Tool '{tool_name}' not found", ActionStatus.NOT_FOUND
        try:
            if asyncio.iscoroutinefunction(tool.fn):
                result = await tool.fn(**args)
            else:
                result = tool.fn(**args)
            self._stats["tool_calls"] += 1
            return result, ActionStatus.SUCCESS
        except Exception as e:
            return str(e), ActionStatus.FAILED

    async def run(self, goal: Goal) -> AgentPlan:
        import secrets

        plan = AgentPlan(
            plan_id=secrets.token_hex(8),
            goal=goal,
        )
        self._plans.append(plan)
        self._stats["plans_created"] += 1

        step_id = 0
        max_steps = min(goal.max_steps, self._max_steps)
        timeout = min(goal.timeout_s, self._timeout)
        deadline = time.time() + timeout

        try:
            while step_id < max_steps and time.time() < deadline:
                prompt = _build_react_prompt(
                    goal, plan.steps, list(self._tools.values())
                )
                llm_response = await self._call_llm(prompt)

                step_type, content, tool_name, tool_args = _parse_react_response(
                    llm_response
                )
                step_id += 1
                self._stats["total_steps"] += 1

                if step_type == StepType.FINAL_ANSWER:
                    plan.final_answer = content
                    plan.status = PlanStatus.SUCCEEDED
                    self._stats["plans_succeeded"] += 1
                    plan.steps.append(
                        Step(
                            step_id=step_id,
                            step_type=StepType.FINAL_ANSWER,
                            content=content,
                        )
                    )
                    break

                elif step_type == StepType.ACTION:
                    t0 = time.time()
                    result, action_status = await self._execute_tool(
                        tool_name, tool_args
                    )
                    dur = (time.time() - t0) * 1000

                    plan.steps.append(
                        Step(
                            step_id=step_id,
                            step_type=StepType.ACTION,
                            content=content,
                            tool_name=tool_name,
                            tool_args=tool_args,
                            tool_result=result,
                            action_status=action_status,
                            duration_ms=dur,
                        )
                    )

                    # Add observation
                    step_id += 1
                    obs_text = (
                        json.dumps(result) if not isinstance(result, str) else result
                    )
                    plan.steps.append(
                        Step(
                            step_id=step_id,
                            step_type=StepType.OBSERVATION,
                            content=obs_text[:500],
                        )
                    )

                else:  # THOUGHT or REFLECTION
                    plan.steps.append(
                        Step(
                            step_id=step_id,
                            step_type=step_type,
                            content=content,
                        )
                    )

            else:
                if step_id >= max_steps:
                    plan.status = PlanStatus.MAX_STEPS
                    plan.error = f"reached max_steps={max_steps}"
                else:
                    plan.status = PlanStatus.FAILED
                    plan.error = "timeout"
                self._stats["plans_failed"] += 1

        except Exception as e:
            plan.status = PlanStatus.FAILED
            plan.error = str(e)
            self._stats["plans_failed"] += 1
            log.error(f"Plan {plan.plan_id} failed: {e}")

        plan.finished_at = time.time()
        log.info(
            f"Plan {plan.plan_id} {plan.status.value} "
            f"steps={plan.step_count} dur={plan.duration_ms:.0f}ms"
        )
        return plan

    def recent_plans(self, limit: int = 10) -> list[dict]:
        return [p.to_dict() for p in self._plans[-limit:]]

    def stats(self) -> dict:
        return {
            **self._stats,
            "tools_registered": len(self._tools),
            "total_plans": len(self._plans),
        }


def build_jarvis_agent_planner(llm_fn=None) -> AgentPlanner:
    planner = AgentPlanner(llm_fn=llm_fn, max_steps=25, timeout_s=120.0)

    @planner.tool(
        "cluster_status", "Get current cluster node health", '{"node": "all|m1|m2"}'
    )
    async def cluster_status(node: str = "all") -> dict:
        return {
            "m1": "healthy",
            "m2": "healthy",
            "ol1": "healthy",
            "queried_node": node,
        }

    @planner.tool("gpu_temps", "Get GPU temperatures", '{"node": "m1"}')
    async def gpu_temps(node: str = "m1") -> dict:
        import random

        return {f"gpu{i}": round(random.uniform(55, 75), 1) for i in range(4)}

    @planner.tool("list_models", "List available LLM models", '{"node": "m1"}')
    async def list_models(node: str = "m1") -> list:
        return ["qwen3.5-9b", "deepseek-r1", "gemma3:4b"]

    @planner.tool("bash", "Execute a shell command", '{"cmd": "ls /tmp"}')
    async def bash_tool(cmd: str = "") -> str:
        import subprocess

        if not cmd:
            return "no command provided"
        # Safety: only allow safe read-only commands
        safe_prefixes = ("ls", "echo", "cat /proc", "df", "free", "uptime", "date")
        if not any(cmd.startswith(p) for p in safe_prefixes):
            return f"command not allowed: {cmd!r}"
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=5
        )  # noqa: S602
        return result.stdout[:500] or result.stderr[:500]

    return planner


async def main():
    import sys

    planner = build_jarvis_agent_planner()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Agent planner demo...\n")

        goal = Goal(
            goal_id="g1",
            description="Check cluster health and report GPU temperatures on M1.",
            success_criteria=["cluster status retrieved", "GPU temps retrieved"],
            max_steps=10,
        )

        plan = await planner.run(goal)

        print(f"  Plan: {plan.plan_id}")
        print(f"  Status: {plan.status.value}")
        print(f"  Steps: {plan.step_count}")
        print(f"  Duration: {plan.duration_ms:.0f}ms")
        print()

        for step in plan.steps:
            icon = {
                StepType.THOUGHT: "💭",
                StepType.ACTION: "⚡",
                StepType.OBSERVATION: "👁",
                StepType.FINAL_ANSWER: "✅",
                StepType.REFLECTION: "🔄",
            }.get(step.step_type, "•")
            print(f"  {icon} [{step.step_type.value:12}] {step.content[:80]}")

        if plan.final_answer:
            print(f"\n  Final: {plan.final_answer}")

        print(f"\nStats: {json.dumps(planner.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(planner.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
