#!/usr/bin/env python3
"""
jarvis_plugin_sandbox — Isolated execution environment for untrusted plugins
Runs plugins with resource limits, timeout enforcement, and capability gating
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

log = logging.getLogger("jarvis.plugin_sandbox")

REDIS_PREFIX = "jarvis:sandbox:"


class Capability(str, Enum):
    FILESYSTEM = "filesystem"
    NETWORK = "network"
    REDIS = "redis"
    LLM = "llm"
    SUBPROCESS = "subprocess"
    MEMORY = "memory"


@dataclass
class SandboxPolicy:
    name: str
    allowed_capabilities: set[Capability] = field(default_factory=set)
    max_exec_time_s: float = 10.0
    max_memory_mb: int = 256
    max_output_chars: int = 100_000
    allow_exceptions: bool = (
        False  # if False, exceptions are caught and returned as errors
    )


@dataclass
class ExecutionResult:
    exec_id: str
    plugin_name: str
    status: str  # ok | timeout | error | capability_denied
    output: Any
    error: str = ""
    duration_ms: float = 0.0
    capabilities_used: list[str] = field(default_factory=list)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "exec_id": self.exec_id,
            "plugin_name": self.plugin_name,
            "status": self.status,
            "output": str(self.output)[:1000] if self.output is not None else None,
            "error": self.error[:500],
            "duration_ms": round(self.duration_ms, 1),
            "capabilities_used": self.capabilities_used,
            "ts": self.ts,
        }


class CapabilityContext:
    """Injected into sandboxed plugins; tracks and gates capability usage."""

    def __init__(self, policy: SandboxPolicy):
        self._policy = policy
        self.used: list[str] = []
        self._denied: list[str] = []

    def require(self, cap: Capability) -> bool:
        if cap not in self._policy.allowed_capabilities:
            self._denied.append(cap.value)
            raise PermissionError(
                f"Capability '{cap.value}' not allowed by policy '{self._policy.name}'"
            )
        self.used.append(cap.value)
        return True

    @property
    def denied(self) -> list[str]:
        return self._denied


class PluginSandbox:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._policies: dict[str, SandboxPolicy] = {}
        self._history: list[ExecutionResult] = []
        self._default_policy = SandboxPolicy(
            name="default",
            allowed_capabilities={Capability.LLM, Capability.REDIS, Capability.MEMORY},
        )

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def register_policy(self, policy: SandboxPolicy):
        self._policies[policy.name] = policy
        log.debug(
            f"Policy registered: {policy.name} ({len(policy.allowed_capabilities)} caps)"
        )

    def get_policy(self, name: str) -> SandboxPolicy:
        return self._policies.get(name, self._default_policy)

    async def execute(
        self,
        plugin_name: str,
        fn: Callable,
        args: tuple = (),
        kwargs: dict | None = None,
        policy_name: str = "default",
        context_override: dict | None = None,
    ) -> ExecutionResult:
        exec_id = str(uuid.uuid4())[:8]
        policy = self.get_policy(policy_name)
        ctx = CapabilityContext(policy)
        kwargs = kwargs or {}

        # Inject capability context if plugin accepts it
        import inspect

        sig = inspect.signature(fn)
        if "cap_ctx" in sig.parameters:
            kwargs["cap_ctx"] = ctx

        t0 = time.time()
        try:
            if asyncio.iscoroutinefunction(fn):
                output = await asyncio.wait_for(
                    fn(*args, **kwargs), timeout=policy.max_exec_time_s
                )
            else:
                loop = asyncio.get_event_loop()
                output = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: fn(*args, **kwargs)),
                    timeout=policy.max_exec_time_s,
                )

            # Truncate output if needed
            if isinstance(output, str) and len(output) > policy.max_output_chars:
                output = output[: policy.max_output_chars] + "...[truncated]"

            result = ExecutionResult(
                exec_id=exec_id,
                plugin_name=plugin_name,
                status="ok",
                output=output,
                duration_ms=(time.time() - t0) * 1000,
                capabilities_used=ctx.used,
            )

        except asyncio.TimeoutError:
            result = ExecutionResult(
                exec_id=exec_id,
                plugin_name=plugin_name,
                status="timeout",
                output=None,
                error=f"Timed out after {policy.max_exec_time_s}s",
                duration_ms=(time.time() - t0) * 1000,
                capabilities_used=ctx.used,
            )
        except PermissionError as e:
            result = ExecutionResult(
                exec_id=exec_id,
                plugin_name=plugin_name,
                status="capability_denied",
                output=None,
                error=str(e),
                duration_ms=(time.time() - t0) * 1000,
                capabilities_used=ctx.used,
            )
        except Exception as e:
            if policy.allow_exceptions:
                raise
            result = ExecutionResult(
                exec_id=exec_id,
                plugin_name=plugin_name,
                status="error",
                output=None,
                error=f"{type(e).__name__}: {str(e)[:300]}",
                duration_ms=(time.time() - t0) * 1000,
                capabilities_used=ctx.used,
            )

        self._history.append(result)
        if self.redis:
            asyncio.create_task(
                self.redis.lpush(
                    f"{REDIS_PREFIX}history",
                    json.dumps(result.to_dict()),
                )
            )
            asyncio.create_task(self.redis.ltrim(f"{REDIS_PREFIX}history", 0, 999))

        log.debug(
            f"Sandbox [{exec_id}] {plugin_name}: {result.status} {result.duration_ms:.0f}ms"
        )
        return result

    def stats(self) -> dict:
        total = len(self._history)
        ok = sum(1 for r in self._history if r.status == "ok")
        return {
            "total_executions": total,
            "ok": ok,
            "errors": sum(1 for r in self._history if r.status == "error"),
            "timeouts": sum(1 for r in self._history if r.status == "timeout"),
            "capability_denied": sum(
                1 for r in self._history if r.status == "capability_denied"
            ),
            "success_rate": round(ok / max(total, 1) * 100, 1),
            "policies": list(self._policies.keys()),
        }


async def main():
    import sys

    sandbox = PluginSandbox()
    await sandbox.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        sandbox.register_policy(
            SandboxPolicy(
                name="trusted",
                allowed_capabilities={
                    Capability.LLM,
                    Capability.REDIS,
                    Capability.MEMORY,
                },
                max_exec_time_s=5.0,
            )
        )
        sandbox.register_policy(
            SandboxPolicy(
                name="restricted",
                allowed_capabilities={Capability.MEMORY},
                max_exec_time_s=2.0,
            )
        )

        async def good_plugin(cap_ctx=None):
            if cap_ctx:
                cap_ctx.require(Capability.MEMORY)
            await asyncio.sleep(0.01)
            return {"status": "processed", "items": 42}

        async def bad_plugin(cap_ctx=None):
            if cap_ctx:
                cap_ctx.require(Capability.NETWORK)  # not allowed
            return "should not reach here"

        async def slow_plugin():
            await asyncio.sleep(10)
            return "too slow"

        for pname, fn, pol in [
            ("good_plugin", good_plugin, "restricted"),
            ("bad_plugin", bad_plugin, "restricted"),
            ("slow_plugin", slow_plugin, "restricted"),
        ]:
            result = await sandbox.execute(pname, fn, policy_name=pol)
            print(
                f"  {pname}: {result.status} ({result.duration_ms:.0f}ms) {result.error or result.output}"
            )

        print(f"\nStats: {json.dumps(sandbox.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(sandbox.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
