#!/usr/bin/env python3
"""
jarvis_function_registry — OpenAI-compatible function/tool registry
Manages tool schemas, dispatches LLM tool-calls to Python functions
"""

import asyncio
import inspect
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.function_registry")

REDIS_KEY = "jarvis:function_registry"
LM_URL = "http://127.0.0.1:1234"


@dataclass
class FunctionSchema:
    name: str
    description: str
    parameters: dict
    handler: Callable
    is_async: bool = False
    call_count: int = 0
    error_count: int = 0
    avg_latency_ms: float = 0.0

    def to_tool_spec(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolCall:
    call_id: str
    function_name: str
    arguments: dict
    result: Any = None
    error: str = ""
    latency_ms: float = 0.0
    ts: float = field(default_factory=time.time)


class FunctionRegistry:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._functions: dict[str, FunctionSchema] = {}
        self._call_history: list[ToolCall] = []
        self._register_builtins()

    def _register_builtins(self):
        """Register built-in JARVIS tools."""
        self.register(
            name="get_gpu_status",
            description="Get current GPU temperatures, power draw, and VRAM usage",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=self._builtin_gpu_status,
        )
        self.register(
            name="get_cluster_status",
            description="Get LLM cluster node availability and loaded models",
            parameters={
                "type": "object",
                "properties": {
                    "node": {
                        "type": "string",
                        "description": "Node name (M1, M2, OL1) or 'all'",
                        "default": "all",
                    }
                },
                "required": [],
            },
            handler=self._builtin_cluster_status,
        )
        self.register(
            name="run_bash",
            description="Execute a bash command and return output (read-only operations only)",
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run"}
                },
                "required": ["command"],
            },
            handler=self._builtin_bash,
        )
        self.register(
            name="redis_get",
            description="Get a value from Redis by key",
            parameters={
                "type": "object",
                "properties": {"key": {"type": "string", "description": "Redis key"}},
                "required": ["key"],
            },
            handler=self._builtin_redis_get,
        )
        self.register(
            name="current_time",
            description="Get current date and time",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=lambda: time.strftime("%Y-%m-%d %H:%M:%S"),
        )

    def register(
        self,
        name: str,
        description: str,
        parameters: dict,
        handler: Callable,
    ) -> FunctionSchema:
        is_async = inspect.iscoroutinefunction(handler)
        schema = FunctionSchema(
            name=name,
            description=description,
            parameters=parameters,
            handler=handler,
            is_async=is_async,
        )
        self._functions[name] = schema
        log.debug(f"Registered function: {name}")
        return schema

    def get_tools(self, names: list[str] | None = None) -> list[dict]:
        """Return OpenAI tool specs for registered functions."""
        fns = self._functions.values()
        if names:
            fns = [f for f in fns if f.name in names]
        return [f.to_tool_spec() for f in fns]

    async def dispatch(self, tool_call: dict) -> ToolCall:
        """Dispatch an LLM tool-call dict to the registered handler."""
        fn_name = tool_call.get("function", {}).get("name", "")
        raw_args = tool_call.get("function", {}).get("arguments", "{}")
        call_id = tool_call.get("id", "unknown")

        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except json.JSONDecodeError:
            args = {}

        tc = ToolCall(call_id=call_id, function_name=fn_name, arguments=args)
        schema = self._functions.get(fn_name)

        if not schema:
            tc.error = f"Function '{fn_name}' not registered"
            return tc

        t0 = time.time()
        try:
            if schema.is_async:
                result = await schema.handler(**args)
            else:
                result = schema.handler(**args)

            tc.result = result
            schema.call_count += 1
        except Exception as e:
            tc.error = str(e)
            schema.error_count += 1
            log.error(f"Tool call '{fn_name}' failed: {e}")
        finally:
            tc.latency_ms = round((time.time() - t0) * 1000, 1)
            alpha = 0.3
            schema.avg_latency_ms = (
                alpha * tc.latency_ms + (1 - alpha) * schema.avg_latency_ms
            )

        self._call_history.append(tc)
        if len(self._call_history) > 500:
            self._call_history = self._call_history[-500:]

        return tc

    async def llm_with_tools(
        self,
        prompt: str,
        model: str = "qwen/qwen3.5-9b",
        tool_names: list[str] | None = None,
        max_rounds: int = 5,
    ) -> str:
        """Run LLM with tool-calling loop until final answer."""
        tools = self.get_tools(tool_names)
        messages = [{"role": "user", "content": prompt}]

        for round_n in range(max_rounds):
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60)
            ) as sess:
                payload: dict = {
                    "model": model,
                    "messages": messages,
                    "max_tokens": 1024,
                    "temperature": 0,
                }
                if tools:
                    payload["tools"] = tools
                    payload["tool_choice"] = "auto"

                async with sess.post(
                    f"{LM_URL}/v1/chat/completions", json=payload
                ) as r:
                    data = await r.json()

            choice = data["choices"][0]
            msg = choice["message"]
            messages.append(msg)

            if choice.get("finish_reason") == "tool_calls":
                tool_calls = msg.get("tool_calls", [])
                for tc_dict in tool_calls:
                    tc = await self.dispatch(tc_dict)
                    result_content = (
                        json.dumps(tc.result) if tc.result is not None else tc.error
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.call_id,
                            "content": result_content,
                        }
                    )
                log.debug(f"Round {round_n + 1}: executed {len(tool_calls)} tool calls")
            else:
                return msg.get("content", "")

        return messages[-1].get("content", "Max rounds reached")

    def stats(self) -> list[dict]:
        return [
            {
                "name": s.name,
                "calls": s.call_count,
                "errors": s.error_count,
                "avg_latency_ms": round(s.avg_latency_ms, 1),
                "success_rate": round(
                    (s.call_count - s.error_count) / max(s.call_count, 1) * 100, 1
                ),
            }
            for s in self._functions.values()
        ]

    # ── Built-in handlers ─────────────────────────────────────────────────────

    async def _builtin_gpu_status(self) -> dict:
        import subprocess

        r = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,temperature.gpu,power.draw,memory.used,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
        )
        gpus = []
        for line in r.stdout.strip().split("\n"):
            if line.strip():
                p = [x.strip() for x in line.split(",")]
                gpus.append(
                    {
                        "idx": p[0],
                        "temp": p[1],
                        "power": p[2],
                        "vram": p[3],
                        "util": p[4],
                    }
                )
        return {"gpus": gpus, "ts": time.strftime("%H:%M:%S")}

    async def _builtin_cluster_status(self, node: str = "all") -> dict:
        nodes = {"M1": "http://127.0.0.1:1234", "M2": "http://192.168.1.26:1234"}
        if node != "all" and node in nodes:
            nodes = {node: nodes[node]}
        result = {}
        for n, url in nodes.items():
            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=3)
                ) as sess:
                    async with sess.get(f"{url}/v1/models") as r:
                        if r.status == 200:
                            data = await r.json()
                            result[n] = {
                                "ok": True,
                                "models": [m["id"] for m in data.get("data", [])[:3]],
                            }
                        else:
                            result[n] = {"ok": False}
            except Exception:
                result[n] = {"ok": False}
        return result

    async def _builtin_bash(self, command: str) -> str:
        import subprocess

        # Safety: block destructive commands
        blocked = ["rm ", "dd ", "mkfs", "fdisk", "> /dev", "shutdown", "reboot"]
        if any(b in command for b in blocked):
            return f"BLOCKED: command '{command}' is potentially destructive"
        r = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=10
        )
        return (r.stdout + r.stderr).strip()[:1000]

    async def _builtin_redis_get(self, key: str) -> str | None:
        if self.redis:
            return await self.redis.get(key)
        return None

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
            # Update builtin handler references
            self._functions["redis_get"].handler = self._builtin_redis_get
        except Exception:
            self.redis = None


async def main():
    import sys

    registry = FunctionRegistry()
    await registry.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"

    if cmd == "list":
        print(f"{'Function':<28} {'Calls':>6} {'Errors':>7} {'Avg ms':>8}")
        print("-" * 55)
        for s in registry.stats():
            print(
                f"{s['name']:<28} {s['calls']:>6} {s['errors']:>7} {s['avg_latency_ms']:>8.1f}"
            )

    elif cmd == "call" and len(sys.argv) > 2:
        fn = sys.argv[2]
        args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
        tc = await registry.dispatch(
            {"id": "cli", "function": {"name": fn, "arguments": json.dumps(args)}}
        )
        if tc.error:
            print(f"Error: {tc.error}")
        else:
            print(
                json.dumps(tc.result, indent=2)
                if isinstance(tc.result, dict)
                else tc.result
            )

    elif cmd == "ask" and len(sys.argv) > 2:
        prompt = " ".join(sys.argv[2:])
        print(f"Asking with tools: {prompt}")
        result = await registry.llm_with_tools(prompt)
        print(result)


if __name__ == "__main__":
    asyncio.run(main())
