#!/usr/bin/env python3
"""JARVIS Chain Executor — Execute sequential/parallel agent chains with shared context"""

import redis
import json
import time
import uuid
import threading

r = redis.Redis(decode_responses=True)

CHAIN_PREFIX = "jarvis:chain:"
RUN_PREFIX = "jarvis:chain:run:"
STATS_KEY = "jarvis:chain:stats"


def define_chain(name: str, agents: list, mode: str = "sequential") -> dict:
    """
    Define an agent chain.
    agents: list of {agent, prompt_template, output_key, condition}
    mode: sequential | parallel | race
    """
    chain = {"name": name, "agents": agents, "mode": mode, "defined_at": time.time()}
    r.set(f"{CHAIN_PREFIX}{name}", json.dumps(chain))
    return chain


def _call_agent(agent_spec: dict, context: dict) -> dict:
    """Mock agent call — real impl delegates to jarvis_llm_router or jarvis_inference_gateway."""
    agent = agent_spec.get("agent", "default")
    prompt_tpl = agent_spec.get("prompt_template", "{{input}}")

    # Interpolate context vars
    prompt = prompt_tpl
    for k, v in context.items():
        prompt = prompt.replace(f"{{{{{k}}}}}", str(v))

    t0 = time.perf_counter()
    try:
        from jarvis_llm_router import ask, detect_type

        task_type = agent_spec.get("task_type", detect_type(prompt))
        result = ask(prompt, task_type)
        lat = round((time.perf_counter() - t0) * 1000)
        return {
            "output": result,
            "ok": bool(result and not result.startswith("[router_error")),
            "latency_ms": lat,
            "agent": agent,
        }
    except Exception as e:
        lat = round((time.perf_counter() - t0) * 1000)
        return {
            "output": "",
            "ok": False,
            "error": str(e)[:80],
            "latency_ms": lat,
            "agent": agent,
        }


def run(name: str, inputs: dict = None) -> dict:
    """Execute a named chain."""
    raw = r.get(f"{CHAIN_PREFIX}{name}")
    if not raw:
        return {"ok": False, "error": f"chain not found: {name}"}

    chain = json.loads(raw)
    run_id = uuid.uuid4().hex[:10]
    context = {**(inputs or {})}
    mode = chain.get("mode", "sequential")
    agents = chain["agents"]
    results = []
    t0 = time.perf_counter()

    if mode == "parallel":
        outputs = [None] * len(agents)
        threads = []

        def run_agent(idx, spec):
            outputs[idx] = _call_agent(spec, context.copy())

        for i, spec in enumerate(agents):
            t = threading.Thread(target=run_agent, args=(i, spec))
            threads.append(t)
            t.start()
        for t in threads:
            t.join(timeout=60)
        results = outputs

    elif mode == "race":
        winner = [None]
        done = threading.Event()

        def run_agent_race(spec):
            result = _call_agent(spec, context.copy())
            if result["ok"] and not done.is_set():
                done.set()
                winner[0] = result

        threads = [threading.Thread(target=run_agent_race, args=(s,)) for s in agents]
        for t in threads:
            t.start()
        done.wait(timeout=30)
        results = [winner[0]] if winner[0] else []

    else:  # sequential
        for spec in agents:
            res = _call_agent(spec, context)
            results.append(res)
            if res["ok"]:
                out_key = spec.get("output_key", f"step_{len(results)}")
                context[out_key] = res["output"]
                context["last_output"] = res["output"]
            elif spec.get("fail_fast", False):
                break

    duration = round((time.perf_counter() - t0) * 1000)
    ok_count = sum(1 for r_ in results if r_ and r_.get("ok"))
    run_record = {
        "id": run_id,
        "chain": name,
        "mode": mode,
        "steps_ok": ok_count,
        "steps_total": len(results),
        "duration_ms": duration,
        "context": context,
        "results": results,
    }
    r.setex(f"{RUN_PREFIX}{run_id}", 3600, json.dumps(run_record))
    r.hincrby(STATS_KEY, f"{name}:runs", 1)
    if ok_count == len(agents):
        r.hincrby(STATS_KEY, f"{name}:success", 1)
    return run_record


def stats() -> dict:
    s = r.hgetall(STATS_KEY)
    chains = [k.replace(CHAIN_PREFIX, "") for k in r.keys(f"{CHAIN_PREFIX}*")]
    return {"chains_defined": len(chains), **{k: int(v) for k, v in s.items()}}


if __name__ == "__main__":
    define_chain(
        "summarize_and_classify",
        agents=[
            {
                "agent": "summarizer",
                "prompt_template": "Summarize in 1 sentence: {{text}}",
                "output_key": "summary",
                "task_type": "summary",
            },
            {
                "agent": "classifier",
                "prompt_template": "Classify this as technical/business/other: {{summary}}",
                "output_key": "category",
                "task_type": "classify",
            },
        ],
        mode="sequential",
    )

    result = run(
        "summarize_and_classify",
        {
            "text": "JARVIS is a multi-agent GPU cluster management system with LLM routing and Redis-based state management."
        },
    )
    print(
        f"Chain: {result['chain']} | {result['steps_ok']}/{result['steps_total']} ok | {result['duration_ms']}ms"
    )
    for i, step in enumerate(result["results"]):
        if step:
            print(
                f"  Step {i + 1} [{step['agent']}]: {str(step.get('output', ''))[:80]}"
            )
    print(f"Stats: {stats()}")
