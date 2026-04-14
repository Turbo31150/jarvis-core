#!/usr/bin/env python3
"""
jarvis_function_caller — LLM function/tool calling with schema validation and dispatch
OpenAI-compatible tool_call format, argument parsing, result injection
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

log = logging.getLogger("jarvis.function_caller")


class ParamType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    ARRAY = "array"
    OBJECT = "object"


class CallStatus(str, Enum):
    SUCCESS = "success"
    SCHEMA_ERROR = "schema_error"
    EXECUTION_ERROR = "execution_error"
    NOT_FOUND = "not_found"
    TIMEOUT = "timeout"


@dataclass
class ParamSchema:
    name: str
    type: ParamType
    description: str = ""
    required: bool = True
    default: Any = None
    enum_values: list[Any] = field(default_factory=list)
    items_type: ParamType | None = None  # for ARRAY


@dataclass
class ToolSchema:
    name: str
    description: str
    parameters: list[ParamSchema] = field(default_factory=list)
    returns_description: str = ""

    def to_openai_dict(self) -> dict:
        props: dict[str, Any] = {}
        required: list[str] = []
        for p in self.parameters:
            prop: dict[str, Any] = {"type": p.type.value, "description": p.description}
            if p.enum_values:
                prop["enum"] = p.enum_values
            if p.type == ParamType.ARRAY and p.items_type:
                prop["items"] = {"type": p.items_type.value}
            props[p.name] = prop
            if p.required:
                required.append(p.name)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": required,
                },
            },
        }


@dataclass
class ToolCall:
    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    raw: str = ""


@dataclass
class ToolResult:
    call_id: str
    tool_name: str
    status: CallStatus
    result: Any = None
    error: str = ""
    duration_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "status": self.status.value,
            "result": self.result,
            "error": self.error,
            "duration_ms": round(self.duration_ms, 2),
        }

    def to_message(self) -> dict:
        """OpenAI tool_result message format."""
        return {
            "role": "tool",
            "tool_call_id": self.call_id,
            "content": json.dumps(self.result)
            if self.result is not None
            else self.error,
        }


class FunctionCaller:
    def __init__(self, timeout_s: float = 30.0):
        self._tools: dict[str, tuple[ToolSchema, Callable]] = {}
        self._timeout = timeout_s
        self._history: list[ToolResult] = []
        self._max_history = 5_000
        self._stats: dict[str, int] = {
            "calls": 0,
            "success": 0,
            "schema_errors": 0,
            "execution_errors": 0,
            "not_found": 0,
        }

    def register(self, schema: ToolSchema, fn: Callable):
        self._tools[schema.name] = (schema, fn)

    def tool(
        self, name: str, description: str, params: list[ParamSchema] | None = None
    ):
        """Decorator for registering tools."""

        def decorator(fn: Callable):
            schema = ToolSchema(
                name=name, description=description, parameters=params or []
            )
            self.register(schema, fn)
            return fn

        return decorator

    def tool_schemas(self) -> list[dict]:
        return [schema.to_openai_dict() for schema, _ in self._tools.values()]

    def _validate_args(self, schema: ToolSchema, args: dict) -> str | None:
        """Returns error string or None if valid."""
        for param in schema.parameters:
            if param.required and param.name not in args:
                if param.default is not None:
                    args[param.name] = param.default
                else:
                    return f"missing required parameter: {param.name!r}"

            val = args.get(param.name)
            if val is None:
                continue

            # Type coercion
            try:
                if param.type == ParamType.INTEGER:
                    args[param.name] = int(val)
                elif param.type == ParamType.NUMBER:
                    args[param.name] = float(val)
                elif param.type == ParamType.BOOLEAN:
                    if isinstance(val, str):
                        args[param.name] = val.lower() in ("true", "1", "yes")
                elif param.type == ParamType.STRING:
                    args[param.name] = str(val)
            except (ValueError, TypeError) as e:
                return f"type error for {param.name!r}: {e}"

            if param.enum_values and args[param.name] not in param.enum_values:
                return f"invalid value for {param.name!r}: must be one of {param.enum_values}"

        return None

    async def execute(self, call: ToolCall) -> ToolResult:
        self._stats["calls"] += 1
        start = time.time()

        entry = self._tools.get(call.tool_name)
        if not entry:
            result = ToolResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                status=CallStatus.NOT_FOUND,
                error=f"tool {call.tool_name!r} not registered",
            )
            self._stats["not_found"] += 1
            self._record(result)
            return result

        schema, fn = entry
        args = dict(call.arguments)

        err = self._validate_args(schema, args)
        if err:
            result = ToolResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                status=CallStatus.SCHEMA_ERROR,
                error=err,
                duration_ms=(time.time() - start) * 1000,
            )
            self._stats["schema_errors"] += 1
            self._record(result)
            return result

        try:
            coro = fn(**args)
            if asyncio.iscoroutine(coro):
                value = await asyncio.wait_for(coro, timeout=self._timeout)
            else:
                value = coro

            result = ToolResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                status=CallStatus.SUCCESS,
                result=value,
                duration_ms=(time.time() - start) * 1000,
            )
            self._stats["success"] += 1
            log.debug(
                f"Tool {call.tool_name!r} [{call.call_id}] OK {result.duration_ms:.0f}ms"
            )

        except asyncio.TimeoutError:
            result = ToolResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                status=CallStatus.TIMEOUT,
                error=f"timeout after {self._timeout}s",
                duration_ms=(time.time() - start) * 1000,
            )
            self._stats["execution_errors"] += 1

        except Exception as e:
            result = ToolResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                status=CallStatus.EXECUTION_ERROR,
                error=str(e),
                duration_ms=(time.time() - start) * 1000,
            )
            self._stats["execution_errors"] += 1
            log.error(f"Tool {call.tool_name!r} error: {e}")

        self._record(result)
        return result

    async def execute_many(self, calls: list[ToolCall]) -> list[ToolResult]:
        tasks = [self.execute(call) for call in calls]
        return list(await asyncio.gather(*tasks))

    def parse_tool_calls(self, llm_response: str) -> list[ToolCall]:
        """Parse tool_calls from LLM JSON response."""
        import secrets

        calls: list[ToolCall] = []

        # Try OpenAI-style JSON
        try:
            data = json.loads(llm_response)
            if isinstance(data, dict) and "tool_calls" in data:
                for tc in data["tool_calls"]:
                    fn = tc.get("function", tc)
                    args = fn.get("arguments", {})
                    if isinstance(args, str):
                        args = json.loads(args)
                    calls.append(
                        ToolCall(
                            call_id=tc.get("id", secrets.token_hex(6)),
                            tool_name=fn.get("name", ""),
                            arguments=args,
                            raw=llm_response,
                        )
                    )
                return calls
        except Exception:
            pass

        # Fallback: scan for ```json blocks with tool_call shape
        blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", llm_response, re.DOTALL)
        for block in blocks:
            try:
                data = json.loads(block)
                if "name" in data and ("arguments" in data or "args" in data):
                    args = data.get("arguments") or data.get("args", {})
                    if isinstance(args, str):
                        args = json.loads(args)
                    calls.append(
                        ToolCall(
                            call_id=secrets.token_hex(6),
                            tool_name=data["name"],
                            arguments=args,
                            raw=block,
                        )
                    )
            except Exception:
                pass

        return calls

    def _record(self, result: ToolResult):
        self._history.append(result)
        if len(self._history) > self._max_history:
            self._history.pop(0)

    def recent(self, limit: int = 20) -> list[dict]:
        return [r.to_dict() for r in self._history[-limit:]]

    def stats(self) -> dict:
        total = self._stats["calls"]
        return {
            **self._stats,
            "tools_registered": len(self._tools),
            "success_rate": round(self._stats["success"] / max(total, 1), 4),
        }


def build_jarvis_function_caller() -> FunctionCaller:
    fc = FunctionCaller(timeout_s=30.0)

    @fc.tool("cluster.status", "Get current cluster health and node status")
    async def cluster_status() -> dict:
        return {
            "nodes": {"m1": "healthy", "m2": "healthy", "ol1": "healthy"},
            "ts": time.time(),
        }

    @fc.tool(
        "model.list",
        "List available LLM models on the cluster",
        [
            ParamSchema(
                "node", ParamType.STRING, "Node to query", required=False, default="m1"
            )
        ],
    )
    async def model_list(node: str = "m1") -> list:
        models = {
            "m1": ["qwen3.5-9b", "qwen3.5-35b", "deepseek-r1", "gemma-4-26b"],
            "m2": ["qwen3.5-9b", "deepseek-r1", "qwen3-8b"],
            "ol1": ["gemma3:4b", "llama3.2"],
        }
        return models.get(node, [])

    @fc.tool(
        "budget.check",
        "Check remaining token budget",
        [ParamSchema("agent", ParamType.STRING, "Agent ID to check")],
    )
    async def budget_check(agent: str) -> dict:
        return {
            "agent": agent,
            "remaining_tokens": 50000,
            "cap": 100000,
            "pct_used": 0.5,
        }

    @fc.tool(
        "gpu.temperature",
        "Get GPU temperatures",
        [
            ParamSchema(
                "node", ParamType.STRING, "Node name", required=False, default="m1"
            )
        ],
    )
    async def gpu_temp(node: str = "m1") -> dict:
        import random

        gpus = {f"GPU{i}": round(random.uniform(55, 75), 1) for i in range(4)}
        return {"node": node, "temperatures": gpus}

    return fc


async def main():
    import sys

    fc = build_jarvis_function_caller()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Function caller demo...\n")

        # Direct calls
        import secrets

        calls = [
            ToolCall(secrets.token_hex(4), "cluster.status", {}),
            ToolCall(secrets.token_hex(4), "model.list", {"node": "m1"}),
            ToolCall(secrets.token_hex(4), "budget.check", {"agent": "inference-gw"}),
            ToolCall(secrets.token_hex(4), "gpu.temperature", {}),
            ToolCall(secrets.token_hex(4), "unknown.tool", {}),
            ToolCall(secrets.token_hex(4), "budget.check", {}),  # missing required arg
        ]

        results = await fc.execute_many(calls)
        for r in results:
            icon = "✅" if r.status == CallStatus.SUCCESS else "❌"
            preview = str(r.result)[:60] if r.result else r.error
            print(f"  {icon} {r.tool_name:<25} [{r.status.value}] {preview}")

        # Parse from LLM-style JSON
        llm_resp = json.dumps(
            {
                "tool_calls": [
                    {
                        "id": "call_abc123",
                        "function": {
                            "name": "gpu.temperature",
                            "arguments": {"node": "m2"},
                        },
                    }
                ]
            }
        )
        parsed = fc.parse_tool_calls(llm_resp)
        print(f"\n  Parsed {len(parsed)} tool call(s) from LLM response")

        print(f"\nTool schemas (count): {len(fc.tool_schemas())}")
        print(f"Stats: {json.dumps(fc.stats(), indent=2)}")

    elif cmd == "schemas":
        print(json.dumps(fc.tool_schemas(), indent=2))

    elif cmd == "stats":
        print(json.dumps(fc.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
