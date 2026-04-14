#!/usr/bin/env python3
"""JARVIS Pipeline Builder — Declarative multi-step AI pipelines with branching and retry"""

import redis
import json
import time
import uuid
import operator

r = redis.Redis(decode_responses=True)

PIPE_PREFIX = "jarvis:pipeline:"
RUN_PREFIX = "jarvis:pipeline:run:"


def define_pipeline(name: str, steps: list, config: dict = None) -> dict:
    """
    Define a pipeline. Each step: {name, type, params, on_error, condition}
    types: llm, transform, condition, redis_get, redis_set, http, parallel
    """
    pipe = {
        "name": name,
        "steps": steps,
        "config": config or {"retry": 2, "timeout": 60, "fail_fast": True},
        "defined_at": time.time(),
        "version": 1,
    }
    r.set(f"{PIPE_PREFIX}{name}", json.dumps(pipe))
    return pipe


def _evaluate_condition(expr: dict, context: dict) -> bool:
    """Safe condition evaluation using structured expressions instead of eval."""
    op = expr.get("op", "eq")
    left_key = expr.get("left", "last_output")
    right = expr.get("right", "")
    left = context.get(left_key, context.get("last_output", ""))

    ops = {
        "eq": operator.eq,
        "ne": operator.ne,
        "gt": operator.gt,
        "lt": operator.lt,
        "contains": lambda a, b: str(b) in str(a),
        "len_gt": lambda a, b: len(str(a)) > int(b),
        "len_lt": lambda a, b: len(str(a)) < int(b),
        "truthy": lambda a, _: bool(a),
    }
    fn = ops.get(op, operator.eq)
    try:
        return bool(fn(left, right))
    except Exception:
        return False


def _execute_step(step: dict, context: dict) -> dict:
    """Execute a single step with context injection."""
    step_type = step.get("type", "noop")
    params = step.get("params", {})

    # Interpolate {{var}} from context
    def interp(v):
        if isinstance(v, str):
            for k, val in context.items():
                v = v.replace(f"{{{{{k}}}}}", str(val))
        return v

    params = {k: interp(v) for k, v in params.items()}

    if step_type == "redis_get":
        val = r.get(params.get("key", ""))
        return {"output": val, "ok": True}

    elif step_type == "redis_set":
        r.setex(params["key"], int(params.get("ttl", 300)), str(params["value"]))
        return {"output": "stored", "ok": True}

    elif step_type == "transform":
        op = params.get("op", "passthrough")
        val = params.get("input", context.get("last_output", ""))
        if op == "upper":
            return {"output": str(val).upper(), "ok": True}
        elif op == "lower":
            return {"output": str(val).lower(), "ok": True}
        elif op == "truncate":
            n = int(params.get("n", 100))
            return {"output": str(val)[:n], "ok": True}
        elif op == "json_parse":
            try:
                return {"output": json.loads(str(val)), "ok": True}
            except Exception:
                return {"output": val, "ok": False, "error": "json_parse_failed"}
        return {"output": val, "ok": True}

    elif step_type == "condition":
        expr = params.get("expr", {})
        result = _evaluate_condition(expr if isinstance(expr, dict) else {}, context)
        return {"output": result, "ok": True, "branch": "true" if result else "false"}

    elif step_type == "emit":
        msg = {
            "step": step["name"],
            "data": params.get("data", context.get("last_output")),
        }
        r.publish("jarvis:pipeline:events", json.dumps(msg))
        return {"output": "emitted", "ok": True}

    elif step_type == "llm":
        prompt = params.get("prompt", "")
        return {"output": f"[llm_mock:{prompt[:30]}]", "ok": True}

    return {"output": None, "ok": True, "note": f"noop:{step_type}"}


def run_pipeline(name: str, inputs: dict = None) -> dict:
    """Execute a defined pipeline."""
    raw = r.get(f"{PIPE_PREFIX}{name}")
    if not raw:
        return {"ok": False, "error": f"pipeline not found: {name}"}

    pipe = json.loads(raw)
    run_id = uuid.uuid4().hex[:12]
    cfg = pipe.get("config", {})
    context = {**(inputs or {}), "pipeline": name, "run_id": run_id}

    run = {
        "id": run_id,
        "pipeline": name,
        "started_at": time.time(),
        "steps": [],
        "status": "running",
        "context": context,
    }

    for step in pipe["steps"]:
        step_name = step.get("name", "?")
        retries = int(cfg.get("retry", 1))
        result = None

        for attempt in range(retries + 1):
            result = _execute_step(step, context)
            if result["ok"]:
                break
            time.sleep(0.05 * (attempt + 1))

        context["last_output"] = result.get("output")
        context[f"step_{step_name}"] = result.get("output")

        run["steps"].append(
            {
                "name": step_name,
                "type": step.get("type"),
                "ok": result["ok"],
                "output": str(result.get("output", ""))[:200],
            }
        )

        if not result["ok"] and cfg.get("fail_fast", True):
            run["status"] = "failed"
            run["error"] = result.get("error", "step failed")
            break
    else:
        run["status"] = "done"

    run["finished_at"] = time.time()
    run["duration_ms"] = round((run["finished_at"] - run["started_at"]) * 1000)
    r.setex(f"{RUN_PREFIX}{run_id}", 3600, json.dumps(run))
    return run


def stats() -> dict:
    pipes = len(r.keys(f"{PIPE_PREFIX}*"))
    runs = len(r.keys(f"{RUN_PREFIX}*"))
    return {"defined_pipelines": pipes, "recent_runs": runs}


if __name__ == "__main__":
    define_pipeline(
        "score_pipeline",
        steps=[
            {
                "name": "get_score",
                "type": "redis_get",
                "params": {"key": "jarvis:score"},
            },
            {
                "name": "truncate",
                "type": "transform",
                "params": {"op": "truncate", "input": "{{last_output}}", "n": 50},
            },
            {"name": "emit", "type": "emit", "params": {"data": "{{last_output}}"}},
        ],
    )

    define_pipeline(
        "text_pipeline",
        steps=[
            {
                "name": "normalize",
                "type": "transform",
                "params": {"op": "lower", "input": "{{text}}"},
            },
            {
                "name": "check_len",
                "type": "condition",
                "params": {"expr": {"op": "len_gt", "left": "last_output", "right": 5}},
            },
            {
                "name": "store",
                "type": "redis_set",
                "params": {
                    "key": "jarvis:pipeline:last_text",
                    "value": "{{last_output}}",
                    "ttl": 60,
                },
            },
        ],
    )

    for pipe_name, inputs in [
        ("score_pipeline", {}),
        ("text_pipeline", {"text": "Hello JARVIS"}),
    ]:
        result = run_pipeline(pipe_name, inputs)
        icon = "✅" if result["status"] == "done" else "❌"
        print(
            f"  {icon} {pipe_name}: {result['status']} in {result['duration_ms']}ms "
            f"({len(result['steps'])} steps)"
        )

    print(f"Stats: {stats()}")
