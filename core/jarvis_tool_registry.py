#!/usr/bin/env python3
"""
jarvis_tool_registry — LLM tool/function registry with JSON schema generation
Registers callable tools, generates OpenAI-compatible function schemas, validates calls
"""

import asyncio
import inspect
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, get_type_hints

log = logging.getLogger("jarvis.tool_registry")


class ToolStatus(str, Enum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    DISABLED = "disabled"


# Python type → JSON schema type mapping
_TYPE_MAP = {
    int: "integer",
    float: "number",
    str: "string",
    bool: "boolean",
    list: "array",
    dict: "object",
    None: "null",
}


def _python_type_to_json(annotation) -> dict:
    origin = getattr(annotation, "__origin__", None)
    if origin is list:
        args = getattr(annotation, "__args__", None)
        items = _python_type_to_json(args[0]) if args else {}
        return {"type": "array", "items": items}
    if origin is dict:
        return {"type": "object"}
    if annotation in _TYPE_MAP:
        return {"type": _TYPE_MAP[annotation]}
    # Handle Optional[T] → Union[T, None]
    if origin is type(None):
        return {"type": "null"}
    try:
        # Union types (Optional)

        if hasattr(annotation, "__args__"):
            non_none = [a for a in annotation.__args__ if a is not type(None)]
            if non_none:
                return _python_type_to_json(non_none[0])
    except Exception:
        pass
    return {"type": "string"}


@dataclass
class ToolParameter:
    name: str
    type_schema: dict
    description: str = ""
    required: bool = True
    default: Any = inspect.Parameter.empty

    def to_schema(self) -> dict:
        s = dict(self.type_schema)
        if self.description:
            s["description"] = self.description
        if self.default is not inspect.Parameter.empty:
            s["default"] = self.default
        return s


@dataclass
class ToolDefinition:
    name: str
    fn: Callable
    description: str
    parameters: list[ToolParameter]
    tags: list[str] = field(default_factory=list)
    status: ToolStatus = ToolStatus.ACTIVE
    version: str = "1.0"
    call_count: int = 0
    error_count: int = 0
    avg_latency_ms: float = 0.0

    def openai_schema(self) -> dict:
        props = {p.name: p.to_schema() for p in self.parameters}
        required = [p.name for p in self.parameters if p.required]
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

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": len(self.parameters),
            "tags": self.tags,
            "status": self.status.value,
            "version": self.version,
            "call_count": self.call_count,
            "error_count": self.error_count,
            "avg_latency_ms": round(self.avg_latency_ms, 1),
        }


def _extract_params(fn: Callable) -> list[ToolParameter]:
    """Auto-extract parameters from function signature and type hints."""
    sig = inspect.signature(fn)
    hints = {}
    try:
        hints = get_type_hints(fn)
    except Exception:
        pass

    params = []
    doc = inspect.getdoc(fn) or ""

    for name, param in sig.parameters.items():
        if name in ("self", "cls", "ctx", "context"):
            continue

        annotation = hints.get(name, str)
        type_schema = _python_type_to_json(annotation)

        # Try to extract description from docstring (Google/NumPy style)
        param_desc = ""
        for line in doc.split("\n"):
            stripped = line.strip()
            if stripped.startswith(f"{name}:") or stripped.startswith(f"{name} ("):
                param_desc = stripped.split(":", 1)[-1].strip()
                break

        has_default = param.default is not inspect.Parameter.empty
        params.append(
            ToolParameter(
                name=name,
                type_schema=type_schema,
                description=param_desc,
                required=not has_default,
                default=param.default if has_default else inspect.Parameter.empty,
            )
        )
    return params


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}
        self._stats: dict[str, int] = {
            "registered": 0,
            "calls": 0,
            "errors": 0,
        }

    def register(
        self,
        fn: Callable,
        name: str | None = None,
        description: str = "",
        tags: list[str] | None = None,
        version: str = "1.0",
    ) -> ToolDefinition:
        tool_name = name or fn.__name__
        desc = description or (inspect.getdoc(fn) or "").split("\n")[0]
        params = _extract_params(fn)

        tool = ToolDefinition(
            name=tool_name,
            fn=fn,
            description=desc,
            parameters=params,
            tags=tags or [],
            version=version,
        )
        self._tools[tool_name] = tool
        self._stats["registered"] += 1
        log.debug(f"Tool registered: {tool_name} ({len(params)} params)")
        return tool

    def tool(
        self,
        name: str | None = None,
        description: str = "",
        tags: list[str] | None = None,
    ):
        """Decorator for registering tools."""

        def decorator(fn: Callable) -> Callable:
            self.register(fn, name=name, description=description, tags=tags)
            return fn

        return decorator

    def unregister(self, name: str):
        self._tools.pop(name, None)

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    async def call(self, name: str, arguments: dict) -> Any:
        tool = self._tools.get(name)
        if not tool:
            raise KeyError(f"Tool '{name}' not found")
        if tool.status == ToolStatus.DISABLED:
            raise RuntimeError(f"Tool '{name}' is disabled")

        self._stats["calls"] += 1
        tool.call_count += 1
        t0 = time.time()

        try:
            if asyncio.iscoroutinefunction(tool.fn):
                result = await tool.fn(**arguments)
            else:
                result = tool.fn(**arguments)
            latency = (time.time() - t0) * 1000
            # EMA
            tool.avg_latency_ms = (
                0.2 * latency + 0.8 * tool.avg_latency_ms
                if tool.avg_latency_ms > 0
                else latency
            )
            return result
        except Exception:
            tool.error_count += 1
            self._stats["errors"] += 1
            raise

    def validate_call(self, name: str, arguments: dict) -> tuple[bool, list[str]]:
        """Validate call arguments against schema. Returns (valid, errors)."""
        tool = self._tools.get(name)
        if not tool:
            return False, [f"Tool '{name}' not found"]

        errors = []
        for param in tool.parameters:
            if param.required and param.name not in arguments:
                errors.append(f"Missing required parameter: {param.name}")
            if param.name in arguments:
                val = arguments[param.name]
                expected_type = param.type_schema.get("type", "string")
                type_check = {
                    "integer": int,
                    "number": (int, float),
                    "string": str,
                    "boolean": bool,
                    "array": list,
                    "object": dict,
                }
                if expected_type in type_check:
                    if not isinstance(val, type_check[expected_type]):
                        errors.append(
                            f"Parameter '{param.name}': expected {expected_type}, got {type(val).__name__}"
                        )
        return len(errors) == 0, errors

    def openai_tools(self, tags: list[str] | None = None) -> list[dict]:
        """Export all active tools as OpenAI-compatible tool list."""
        tools = [
            t
            for t in self._tools.values()
            if t.status == ToolStatus.ACTIVE
            and (tags is None or any(tag in t.tags for tag in tags))
        ]
        return [t.openai_schema() for t in tools]

    def list_tools(self) -> list[dict]:
        return [t.to_dict() for t in self._tools.values()]

    def stats(self) -> dict:
        active = sum(1 for t in self._tools.values() if t.status == ToolStatus.ACTIVE)
        return {**self._stats, "total": len(self._tools), "active": active}


# Global registry
_registry = ToolRegistry()


def get_registry() -> ToolRegistry:
    return _registry


def build_jarvis_tools() -> ToolRegistry:
    registry = ToolRegistry()

    @registry.tool(tags=["cluster"])
    async def get_gpu_temperature(gpu_id: int = 0) -> dict:
        """Get GPU temperature for a specific GPU.

        gpu_id: GPU index (0-based)
        """
        await asyncio.sleep(0.001)
        temps = {0: 65, 1: 58, 2: 63, 3: 55}
        return {"gpu_id": gpu_id, "temperature_c": temps.get(gpu_id, 0), "status": "ok"}

    @registry.tool(tags=["cluster"])
    async def list_models(node: str = "m1") -> dict:
        """List available models on a cluster node.

        node: Cluster node name (m1, m2, ol1)
        """
        await asyncio.sleep(0.001)
        models = {
            "m1": ["qwen3.5-9b", "qwen3.5-27b"],
            "m2": ["deepseek-r1", "qwen3.5-9b"],
            "ol1": ["gemma3:4b"],
        }
        return {"node": node, "models": models.get(node, [])}

    @registry.tool(tags=["system"])
    def get_timestamp() -> dict:
        """Get current UTC timestamp."""
        return {
            "ts": time.time(),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    @registry.tool(tags=["redis"])
    async def redis_get(key: str) -> dict:
        """Get a value from Redis.

        key: Redis key to retrieve
        """
        await asyncio.sleep(0.001)
        return {"key": key, "value": None, "found": False}

    return registry


async def main():
    import sys

    registry = build_jarvis_tools()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Registered tools:")
        for t in registry.list_tools():
            print(f"  {t['name']:<25} params={t['parameters']} tags={t['tags']}")

        # Call a tool
        result = await registry.call("get_gpu_temperature", {"gpu_id": 0})
        print(f"\nCall get_gpu_temperature(gpu_id=0): {result}")

        # Validate
        valid, errors = registry.validate_call(
            "get_gpu_temperature", {"gpu_id": "not_an_int"}
        )
        print(f"Validation: valid={valid} errors={errors}")

        # OpenAI schema
        schemas = registry.openai_tools(tags=["cluster"])
        print(f"\nOpenAI schemas ({len(schemas)} tools):")
        for s in schemas:
            print(
                f"  {s['function']['name']}: {list(s['function']['parameters']['properties'].keys())}"
            )

        print(f"\nStats: {json.dumps(registry.stats(), indent=2)}")

    elif cmd == "list":
        for t in registry.list_tools():
            print(f"  {t['name']:<25} {t['status']}")

    elif cmd == "stats":
        print(json.dumps(registry.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
