#!/usr/bin/env python3
"""JARVIS Tool Executor — Execute tools/functions from LLM tool-call responses"""

import redis
import json
import subprocess
import time
import requests
from datetime import datetime

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:tools"

# Tool registry
TOOLS = {
    "bash": {
        "description": "Execute a bash command",
        "params": ["command"],
        "fn": lambda p: subprocess.run(p["command"], shell=True, capture_output=True, text=True, timeout=30).stdout[:500],
    },
    "redis_get": {
        "description": "Get a Redis key value",
        "params": ["key"],
        "fn": lambda p: r.get(p["key"]) or "null",
    },
    "redis_set": {
        "description": "Set a Redis key value",
        "params": ["key", "value"],
        "fn": lambda p: str(r.set(p["key"], p["value"])),
    },
    "http_get": {
        "description": "Perform an HTTP GET request",
        "params": ["url"],
        "fn": lambda p: requests.get(p["url"], timeout=10).text[:500],
    },
    "jarvis_score": {
        "description": "Get JARVIS system score",
        "params": [],
        "fn": lambda p: json.loads(r.get("jarvis:score") or "{}"),
    },
    "jarvis_health": {
        "description": "Get JARVIS health aggregation",
        "params": [],
        "fn": lambda p: json.loads(r.get("jarvis:health:last") or "{}"),
    },
    "memory_recall": {
        "description": "Recall from JARVIS memory store",
        "params": ["key", "namespace"],
        "fn": lambda p: __import__("jarvis_memory_store", fromlist=["recall"]).recall(p["key"], p.get("namespace", "agent")),
    },
    "infer": {
        "description": "Call LLM inference",
        "params": ["prompt", "task_type"],
        "fn": lambda p: __import__("jarvis_inference_gateway", fromlist=["infer"]).infer(p["prompt"], p.get("task_type", "fast"))["response"],
    },
}


def execute(tool_name: str, params: dict) -> dict:
    if tool_name not in TOOLS:
        return {"ok": False, "error": f"Unknown tool: {tool_name}", "available": list(TOOLS.keys())}
    tool = TOOLS[tool_name]
    t0 = time.perf_counter()
    try:
        result = tool["fn"](params)
        ms = round((time.perf_counter() - t0) * 1000)
        r.hincrby(f"{PREFIX}:stats:{tool_name}", "calls", 1)
        entry = {"tool": tool_name, "params": params, "result": str(result)[:200], "ms": ms, "ok": True, "ts": datetime.now().isoformat()[:19]}
        r.lpush(f"{PREFIX}:log", json.dumps(entry))
        r.ltrim(f"{PREFIX}:log", 0, 199)
        return {"ok": True, "result": result, "tool": tool_name, "ms": ms}
    except Exception as e:
        ms = round((time.perf_counter() - t0) * 1000)
        r.hincrby(f"{PREFIX}:stats:{tool_name}", "errors", 1)
        return {"ok": False, "error": str(e)[:80], "tool": tool_name, "ms": ms}


def execute_tool_calls(tool_calls: list) -> list:
    """Execute a list of tool calls (from LLM response format)"""
    results = []
    for call in tool_calls:
        name = call.get("name") or call.get("function", {}).get("name", "")
        params = call.get("parameters") or call.get("function", {}).get("arguments", {})
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except Exception:
                params = {}
        results.append(execute(name, params))
    return results


def list_tools() -> dict:
    return {name: {"description": cfg["description"], "params": cfg["params"]} for name, cfg in TOOLS.items()}


def stats() -> dict:
    result = {}
    for name in TOOLS:
        data = r.hgetall(f"{PREFIX}:stats:{name}")
        if data:
            result[name] = {"calls": int(data.get("calls", 0)), "errors": int(data.get("errors", 0))}
    return result


if __name__ == "__main__":
    tests = [
        ("redis_get", {"key": "jarvis:score"}),
        ("bash", {"command": "echo 'JARVIS tool test'"}),
        ("jarvis_score", {}),
    ]
    for tool_name, params in tests:
        res = execute(tool_name, params)
        icon = "✅" if res["ok"] else "❌"
        print(f"  {icon} {tool_name}: {str(res.get('result',''))[:60]} ({res['ms']}ms)")
    print(f"\nAvailable tools: {list(list_tools().keys())}")
