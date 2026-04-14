#!/usr/bin/env python3
"""JARVIS Pipeline Executor — Execute multi-step processing pipelines"""

import redis
import json
import time
from datetime import datetime
from typing import Any, Callable

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:pipeline"


class PipelineStep:
    def __init__(self, name: str, fn: Callable, required: bool = True, timeout_s: int = 30):
        self.name = name
        self.fn = fn
        self.required = required
        self.timeout_s = timeout_s


class Pipeline:
    def __init__(self, name: str, steps: list[PipelineStep], parallel: bool = False):
        self.name = name
        self.steps = steps
        self.parallel = parallel

    def execute(self, context: dict = None) -> dict:
        ctx = context or {}
        results = {}
        t_total = time.time()
        errors = []

        if self.parallel:
            import threading
            threads = []
            def _run_step(step):
                t0 = time.time()
                try:
                    out = step.fn(ctx)
                    results[step.name] = {"ok": True, "output": out, "ms": round((time.time()-t0)*1000)}
                    ctx[step.name] = out
                except Exception as e:
                    results[step.name] = {"ok": False, "error": str(e)[:60], "ms": round((time.time()-t0)*1000)}
                    if step.required:
                        errors.append(step.name)
            for step in self.steps:
                t = threading.Thread(target=_run_step, args=(step,))
                t.start()
                threads.append(t)
            for t in threads:
                t.join(timeout=30)
        else:
            for step in self.steps:
                t0 = time.time()
                try:
                    out = step.fn(ctx)
                    results[step.name] = {"ok": True, "output": out, "ms": round((time.time()-t0)*1000)}
                    ctx[step.name] = out
                except Exception as e:
                    results[step.name] = {"ok": False, "error": str(e)[:60], "ms": round((time.time()-t0)*1000)}
                    if step.required:
                        errors.append(step.name)
                        break  # Stop on required failure

        total_ms = round((time.time() - t_total) * 1000)
        ok = len(errors) == 0
        summary = {
            "pipeline": self.name,
            "ts": datetime.now().isoformat()[:19],
            "ok": ok,
            "steps": len(self.steps),
            "errors": errors,
            "total_ms": total_ms,
            "parallel": self.parallel,
            "results": results,
        }
        r.lpush(f"{PREFIX}:runs:{self.name}", json.dumps(summary))
        r.ltrim(f"{PREFIX}:runs:{self.name}", 0, 49)
        return summary


# Predefined pipelines
def build_health_pipeline() -> Pipeline:
    return Pipeline("health_check", [
        PipelineStep("score", lambda ctx: json.loads(r.get("jarvis:score") or "{}")),
        PipelineStep("canary", lambda ctx: json.loads(r.get("jarvis:canary:last") or "{}")),
        PipelineStep("mesh", lambda ctx: json.loads(r.get("jarvis:mesh:summary") or "{}")),
    ], parallel=True)


def build_llm_pipeline() -> Pipeline:
    def preprocess(ctx):
        prompt = ctx.get("prompt", "")
        return {"cleaned": prompt.strip(), "word_count": len(prompt.split())}

    def cache_check(ctx):
        from jarvis_llm_cache import get as cache_get
        prompt = ctx.get("preprocess", {}).get("cleaned", "")
        cached = cache_get(prompt)
        return {"hit": cached is not None, "cached_response": cached}

    def route(ctx):
        from jarvis_llm_router import detect_type
        prompt = ctx.get("preprocess", {}).get("cleaned", "")
        return detect_type(prompt)

    return Pipeline("llm_request", [
        PipelineStep("preprocess", preprocess),
        PipelineStep("cache_check", cache_check, required=False),
        PipelineStep("route", route),
    ], parallel=False)


if __name__ == "__main__":
    # Test health pipeline
    health_pipe = build_health_pipeline()
    res = health_pipe.execute()
    print(f"Health pipeline: {'✅' if res['ok'] else '❌'} {res['total_ms']}ms (parallel)")
    for step_name, step_res in res["results"].items():
        icon = "✅" if step_res["ok"] else "❌"
        print(f"  {icon} {step_name}: {step_res['ms']}ms")

    # Test LLM pipeline
    llm_pipe = build_llm_pipeline()
    res2 = llm_pipe.execute({"prompt": "What is 2+2?"})
    print(f"\nLLM pipeline: {'✅' if res2['ok'] else '❌'} {res2['total_ms']}ms")
    for step_name, step_res in res2["results"].items():
        icon = "✅" if step_res["ok"] else "❌"
        out = step_res.get("output", step_res.get("error", ""))
        print(f"  {icon} {step_name}: {str(out)[:60]}")
