#!/usr/bin/env python3
"""
jarvis_skill_executor — Skill registry and execution engine
Load, invoke, chain, and sandbox skill functions with timeout, retry, rate limiting.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

log = logging.getLogger("jarvis.skill_executor")


class SkillStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    DISABLED = "disabled"


class SkillCategory(str, Enum):
    SYSTEM = "system"
    INFERENCE = "inference"
    DATA = "data"
    TRADING = "trading"
    AUTOMATION = "automation"
    COMMUNICATION = "communication"
    ANALYSIS = "analysis"
    UTILITY = "utility"


@dataclass
class SkillDef:
    name: str
    fn: Callable
    category: SkillCategory = SkillCategory.UTILITY
    description: str = ""
    version: str = "1.0"
    timeout_s: float = 30.0
    max_retries: int = 0
    rate_limit_per_min: int = 0     # 0 = unlimited
    enabled: bool = True
    requires: list[str] = field(default_factory=list)  # dependency skill names
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "category": self.category.value,
            "description": self.description,
            "version": self.version,
            "timeout_s": self.timeout_s,
            "enabled": self.enabled,
        }


@dataclass
class SkillInvocation:
    invocation_id: str
    skill_name: str
    args: dict[str, Any]
    status: SkillStatus = SkillStatus.IDLE
    result: Any = None
    error: str = ""
    retry_count: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0
    duration_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "invocation_id": self.invocation_id,
            "skill_name": self.skill_name,
            "status": self.status.value,
            "duration_ms": round(self.duration_ms, 2),
            "retry_count": self.retry_count,
            "error": self.error,
        }


@dataclass
class ChainResult:
    chain_id: str
    steps: list[SkillInvocation]
    final_result: Any = None
    success: bool = False
    error: str = ""
    total_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "chain_id": self.chain_id,
            "success": self.success,
            "steps": len(self.steps),
            "total_ms": round(self.total_ms, 2),
            "error": self.error,
        }


class RateLimiter:
    def __init__(self, max_per_min: int):
        self._max = max_per_min
        self._calls: list[float] = []

    def check(self) -> bool:
        if self._max <= 0:
            return True
        now = time.time()
        self._calls = [t for t in self._calls if now - t < 60.0]
        if len(self._calls) >= self._max:
            return False
        self._calls.append(now)
        return True


class SkillExecutor:
    """
    Registry and executor for JARVIS skills.
    Supports single invocation, retry, timeout, chaining, and parallel execution.
    """

    def __init__(self):
        self._skills: dict[str, SkillDef] = {}
        self._rate_limiters: dict[str, RateLimiter] = {}
        self._history: list[SkillInvocation] = []
        self._max_history = 1000
        self._stats: dict[str, int] = {
            "invocations": 0,
            "success": 0,
            "failed": 0,
            "timeout": 0,
            "rate_limited": 0,
            "retried": 0,
        }

    def _new_id(self) -> str:
        import secrets
        return secrets.token_hex(6)

    def register(self, skill: SkillDef):
        self._skills[skill.name] = skill
        if skill.rate_limit_per_min > 0:
            self._rate_limiters[skill.name] = RateLimiter(skill.rate_limit_per_min)
        log.debug(f"Registered skill: {skill.name} [{skill.category.value}]")

    def skill(
        self,
        name: str,
        category: SkillCategory = SkillCategory.UTILITY,
        description: str = "",
        timeout_s: float = 30.0,
        max_retries: int = 0,
        rate_limit_per_min: int = 0,
    ):
        """Decorator for registering a skill."""
        def decorator(fn: Callable) -> Callable:
            self.register(SkillDef(
                name=name,
                fn=fn,
                category=category,
                description=description,
                timeout_s=timeout_s,
                max_retries=max_retries,
                rate_limit_per_min=rate_limit_per_min,
            ))
            return fn
        return decorator

    def unregister(self, name: str) -> bool:
        return bool(self._skills.pop(name, None))

    def enable(self, name: str, enabled: bool = True):
        if name in self._skills:
            self._skills[name].enabled = enabled

    async def invoke(
        self, name: str, **kwargs
    ) -> SkillInvocation:
        inv = SkillInvocation(
            invocation_id=self._new_id(),
            skill_name=name,
            args=kwargs,
        )

        skill = self._skills.get(name)
        if not skill:
            inv.status = SkillStatus.FAILED
            inv.error = f"skill '{name}' not found"
            self._record(inv)
            return inv

        if not skill.enabled:
            inv.status = SkillStatus.DISABLED
            inv.error = "skill disabled"
            self._record(inv)
            return inv

        # Rate limit check
        rl = self._rate_limiters.get(name)
        if rl and not rl.check():
            inv.status = SkillStatus.RATE_LIMITED
            inv.error = "rate limit exceeded"
            self._stats["rate_limited"] += 1
            self._record(inv)
            return inv

        # Check dependencies
        for dep in skill.requires:
            if dep not in self._skills:
                inv.status = SkillStatus.FAILED
                inv.error = f"missing dependency: {dep}"
                self._record(inv)
                return inv

        # Execute with retry
        self._stats["invocations"] += 1
        attempt = 0
        while attempt <= skill.max_retries:
            inv.started_at = time.time()
            inv.status = SkillStatus.RUNNING
            try:
                coro = self._call(skill, kwargs)
                inv.result = await asyncio.wait_for(coro, timeout=skill.timeout_s)
                inv.status = SkillStatus.SUCCESS
                self._stats["success"] += 1
                break
            except asyncio.TimeoutError:
                inv.status = SkillStatus.TIMEOUT
                inv.error = f"timeout after {skill.timeout_s}s"
                self._stats["timeout"] += 1
                break
            except Exception as e:
                attempt += 1
                inv.retry_count = attempt
                if attempt <= skill.max_retries:
                    self._stats["retried"] += 1
                    await asyncio.sleep(0.1 * attempt)
                else:
                    inv.status = SkillStatus.FAILED
                    inv.error = str(e)
                    self._stats["failed"] += 1

        inv.finished_at = time.time()
        inv.duration_ms = (inv.finished_at - inv.started_at) * 1000
        self._record(inv)
        return inv

    async def _call(self, skill: SkillDef, kwargs: dict) -> Any:
        if asyncio.iscoroutinefunction(skill.fn):
            return await skill.fn(**kwargs)
        return skill.fn(**kwargs)

    async def chain(
        self,
        steps: list[tuple[str, dict]],
        pass_result: bool = True,
    ) -> ChainResult:
        """
        Execute skills sequentially. If pass_result=True, the result of
        each step is injected as 'previous_result' into the next step's args.
        """
        import secrets
        t0 = time.time()
        cr = ChainResult(chain_id=secrets.token_hex(6), steps=[])
        last_result = None

        for skill_name, kwargs in steps:
            if pass_result and last_result is not None:
                kwargs = {**kwargs, "previous_result": last_result}
            inv = await self.invoke(skill_name, **kwargs)
            cr.steps.append(inv)
            if inv.status != SkillStatus.SUCCESS:
                cr.success = False
                cr.error = f"step '{skill_name}' failed: {inv.error}"
                break
            last_result = inv.result
        else:
            cr.success = True
            cr.final_result = last_result

        cr.total_ms = (time.time() - t0) * 1000
        return cr

    async def parallel(
        self,
        calls: list[tuple[str, dict]],
    ) -> list[SkillInvocation]:
        """Execute multiple skills concurrently."""
        tasks = [self.invoke(name, **kwargs) for name, kwargs in calls]
        return list(await asyncio.gather(*tasks))

    def list_skills(self, category: SkillCategory | None = None) -> list[dict]:
        skills = self._skills.values()
        if category:
            skills = [s for s in skills if s.category == category]
        return [s.to_dict() for s in skills]

    def _record(self, inv: SkillInvocation):
        self._history.append(inv)
        if len(self._history) > self._max_history:
            self._history.pop(0)

    def recent(self, limit: int = 10) -> list[dict]:
        return [i.to_dict() for i in self._history[-limit:]]

    def stats(self) -> dict:
        return {
            **self._stats,
            "registered_skills": len(self._skills),
            "history_size": len(self._history),
        }


def build_jarvis_skill_executor() -> SkillExecutor:
    executor = SkillExecutor()

    @executor.skill("cluster_ping", SkillCategory.SYSTEM, "Ping cluster nodes", timeout_s=5.0)
    async def cluster_ping(node: str = "m1") -> dict:
        await asyncio.sleep(0.01)
        return {"node": node, "status": "ok", "latency_ms": 12}

    @executor.skill("list_models", SkillCategory.INFERENCE, "List loaded LLM models", timeout_s=10.0)
    async def list_models(node: str = "m1") -> list:
        await asyncio.sleep(0.02)
        return ["qwen3.5-9b", "deepseek-r1", "gemma3:4b"]

    @executor.skill(
        "compute_embedding",
        SkillCategory.INFERENCE,
        "Generate text embedding (mock)",
        timeout_s=15.0,
        rate_limit_per_min=60,
    )
    async def compute_embedding(text: str = "") -> list[float]:
        import random
        await asyncio.sleep(0.03)
        return [round(random.gauss(0, 1), 4) for _ in range(8)]

    @executor.skill("gpu_status", SkillCategory.SYSTEM, "Get GPU utilization", timeout_s=5.0)
    async def gpu_status(node: str = "m1") -> dict:
        import random
        await asyncio.sleep(0.01)
        return {f"gpu{i}": {"util": random.randint(20, 80), "temp": random.randint(45, 70)} for i in range(2)}

    @executor.skill("summarize", SkillCategory.ANALYSIS, "Summarize text (mock)", timeout_s=20.0)
    async def summarize(text: str = "", max_words: int = 50, previous_result: Any = None) -> str:
        words = (text or str(previous_result or "")).split()
        return " ".join(words[:max_words]) + ("..." if len(words) > max_words else "")

    return executor


async def main():
    import sys
    executor = build_jarvis_skill_executor()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Skill executor demo...\n")

        # Single invoke
        inv = await executor.invoke("cluster_ping", node="m1")
        print(f"  cluster_ping: {inv.status.value} → {inv.result} ({inv.duration_ms:.1f}ms)")

        # Parallel
        results = await executor.parallel([
            ("gpu_status", {"node": "m1"}),
            ("list_models", {"node": "m2"}),
            ("cluster_ping", {"node": "ol1"}),
        ])
        for r in results:
            print(f"  parallel [{r.skill_name}]: {r.status.value} ({r.duration_ms:.1f}ms)")

        # Chain: embed → summarize
        chain_res = await executor.chain([
            ("cluster_ping", {"node": "m1"}),
            ("summarize", {"text": "JARVIS cluster health check results from node m1"}),
        ], pass_result=True)
        print(f"\n  chain: success={chain_res.success} total={chain_res.total_ms:.1f}ms")
        print(f"  final: {chain_res.final_result}")

        print(f"\n  Skills registered:")
        for s in executor.list_skills():
            print(f"    {s['name']:<25} [{s['category']}]  {s['description']}")

        print(f"\nStats: {json.dumps(executor.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(executor.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
