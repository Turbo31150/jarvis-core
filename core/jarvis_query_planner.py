#!/usr/bin/env python3
"""JARVIS Query Planner — Plan and execute multi-step LLM queries"""

import redis
import json
import time
from datetime import datetime

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:qplan"

PLAN_TEMPLATES = {
    "research": [
        {"step": "classify", "prompt_template": "Classify this query into one of: technical, factual, creative, analytical: {query}", "output_key": "query_type"},
        {"step": "decompose", "prompt_template": "Break down this query into 3 specific sub-questions: {query}", "output_key": "sub_questions"},
        {"step": "answer_each", "prompt_template": "Answer concisely: {sub_questions}", "output_key": "answers"},
        {"step": "synthesize", "prompt_template": "Synthesize these answers into a coherent response for: {query}\nAnswers: {answers}", "output_key": "final"},
    ],
    "code_review": [
        {"step": "understand", "prompt_template": "What does this code do in one sentence: {query}", "output_key": "purpose"},
        {"step": "issues", "prompt_template": "List potential bugs or issues in: {query}", "output_key": "issues"},
        {"step": "improve", "prompt_template": "Suggest improvements for: {query}\nKnown issues: {issues}", "output_key": "suggestions"},
    ],
    "simple": [
        {"step": "answer", "prompt_template": "{query}", "output_key": "final"},
    ],
}


def _fill_template(template: str, ctx: dict) -> str:
    try:
        return template.format(**ctx)
    except KeyError:
        return template.replace("{query}", ctx.get("query", ""))


def execute_plan(query: str, plan_type: str = "simple", backend: str = "auto") -> dict:
    steps = PLAN_TEMPLATES.get(plan_type, PLAN_TEMPLATES["simple"])
    ctx = {"query": query}
    results = []
    t_total = time.time()

    for step_cfg in steps:
        prompt = _fill_template(step_cfg["prompt_template"], ctx)
        t0 = time.time()
        try:
            from jarvis_llm_router import ask, detect_type
            # Route based on step type
            task_type = "fast" if step_cfg["step"] in ("classify",) else "default"
            response = ask(prompt, task_type)
            ctx[step_cfg["output_key"]] = response
            results.append({
                "step": step_cfg["step"],
                "ok": True,
                "output_key": step_cfg["output_key"],
                "ms": round((time.time() - t0) * 1000),
                "response_len": len(response),
            })
        except Exception as e:
            results.append({"step": step_cfg["step"], "ok": False, "error": str(e)[:60]})
            break

    plan_result = {
        "ts": datetime.now().isoformat()[:19],
        "query": query[:80],
        "plan_type": plan_type,
        "steps": len(results),
        "ok_steps": sum(1 for s in results if s.get("ok")),
        "total_ms": round((time.time() - t_total) * 1000),
        "results": results,
        "final_answer": ctx.get("final", ctx.get("suggestions", ctx.get("answers", ""))),
    }
    r.lpush(f"{PREFIX}:history", json.dumps(plan_result))
    r.ltrim(f"{PREFIX}:history", 0, 49)
    return plan_result


def stats() -> dict:
    raw = r.lrange(f"{PREFIX}:history", 0, 19)
    plans = [json.loads(p) for p in raw]
    by_type = {}
    for p in plans:
        t = p["plan_type"]
        by_type[t] = by_type.get(t, 0) + 1
    avg_ms = sum(p["total_ms"] for p in plans) // max(len(plans), 1)
    return {"total_plans": len(plans), "by_type": by_type, "avg_ms": avg_ms}


if __name__ == "__main__":
    print("Testing simple plan...")
    res = execute_plan("What is Redis?", "simple")
    print(f"  Steps: {res['ok_steps']}/{res['steps']} | {res['total_ms']}ms")
    if res["final_answer"]:
        print(f"  Answer: {res['final_answer'][:100]}")
    print(f"Stats: {stats()}")
