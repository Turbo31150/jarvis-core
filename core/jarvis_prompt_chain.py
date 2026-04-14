#!/usr/bin/env python3
"""JARVIS Prompt Chain — Sequential multi-step LLM chains with context passing"""

import redis
import json
import time
import hashlib
import requests

r = redis.Redis(decode_responses=True)

CHAIN_PREFIX = "jarvis:pchain:"
RUN_PREFIX = "jarvis:pchain:run:"
STATS_KEY = "jarvis:pchain:stats"

LLM_URL = "http://192.168.1.113:1234/v1/chat/completions"
LLM_MODEL = "mistral-7b-instruct-v0.3"


def _llm_call(prompt: str, system: str = "", context: str = "") -> str:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    if context:
        messages.append({"role": "assistant", "content": f"[Context]: {context}"})
    messages.append({"role": "user", "content": prompt})
    try:
        resp = requests.post(
            LLM_URL,
            json={
                "model": LLM_MODEL,
                "messages": messages,
                "max_tokens": 256,
                "temperature": 0.3,
            },
            timeout=30,
        )
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"[LLM ERROR: {str(e)[:50]}]"


def define_chain(name: str, steps: list) -> str:
    """
    steps: [{"name": "step1", "prompt_template": "Summarize: {input}",
              "system": "You are concise.", "output_key": "summary"}]
    """
    cid = hashlib.md5(name.encode()).hexdigest()[:10]
    chain = {"id": cid, "name": name, "steps": steps, "created_at": time.time()}
    r.setex(f"{CHAIN_PREFIX}{cid}", 86400 * 30, json.dumps(chain))
    r.hincrby(STATS_KEY, "chains_defined", 1)
    return cid


def run_chain(name: str, initial_input: str, dry_run: bool = False) -> dict:
    cid = hashlib.md5(name.encode()).hexdigest()[:10]
    raw = r.get(f"{CHAIN_PREFIX}{cid}")
    if not raw:
        return {"error": f"Chain '{name}' not found"}

    chain = json.loads(raw)
    run_id = hashlib.md5(f"{cid}:{time.time()}".encode()).hexdigest()[:8]
    context = {}
    context["input"] = initial_input
    steps_results = []

    for step in chain["steps"]:
        t0 = time.perf_counter()
        prompt = step["prompt_template"].format(**context)
        system = step.get("system", "")

        prev_output = context.get(step.get("input_key", "input"), initial_input)

        if dry_run:
            output = f"[DRY RUN] {step['name']}: {prompt[:60]}..."
        else:
            output = _llm_call(prompt, system, prev_output)

        lat = round((time.perf_counter() - t0) * 1000)
        output_key = step.get("output_key", step["name"])
        context[output_key] = output

        steps_results.append(
            {
                "step": step["name"],
                "prompt_len": len(prompt),
                "output_len": len(output),
                "output": output[:200],
                "latency_ms": lat,
            }
        )

    result = {
        "run_id": run_id,
        "chain": name,
        "input": initial_input,
        "output": context.get(chain["steps"][-1].get("output_key", "output"), ""),
        "steps": steps_results,
        "ts": time.time(),
    }
    r.setex(f"{RUN_PREFIX}{run_id}", 3600, json.dumps(result))
    r.hincrby(STATS_KEY, "chains_run", 1)
    return result


def get_run(run_id: str) -> dict | None:
    raw = r.get(f"{RUN_PREFIX}{run_id}")
    return json.loads(raw) if raw else None


def stats() -> dict:
    s = r.hgetall(STATS_KEY)
    return {k: int(v) for k, v in s.items()}


if __name__ == "__main__":
    define_chain(
        "summarize_and_title",
        steps=[
            {
                "name": "summarize",
                "prompt_template": "Summarize in 2 sentences: {input}",
                "system": "You are a concise summarizer.",
                "output_key": "summary",
            },
            {
                "name": "title",
                "prompt_template": "Generate a short title (max 8 words) for: {summary}",
                "system": "You generate brief titles.",
                "output_key": "title",
            },
        ],
    )

    define_chain(
        "analyze_and_recommend",
        steps=[
            {
                "name": "analyze",
                "prompt_template": "Analyze the following briefly: {input}",
                "output_key": "analysis",
            },
            {
                "name": "recommend",
                "prompt_template": "Based on this analysis: {analysis}\nGive 2 actionable recommendations.",
                "output_key": "recommendations",
            },
        ],
    )

    # Dry run first
    print("Chain: summarize_and_title (dry run)")
    result = run_chain(
        "summarize_and_title",
        "JARVIS is a multi-agent AI cluster with M1, M2, M32, OL1 nodes.",
        dry_run=True,
    )
    for step in result["steps"]:
        print(f"  [{step['step']}] {step['output'][:80]}")

    # Live run
    print("\nChain: analyze_and_recommend (live)")
    result = run_chain(
        "analyze_and_recommend",
        "M2 node has 30% error rate and 2500ms average latency.",
    )
    for step in result["steps"]:
        print(f"  [{step['step']}] {step['latency_ms']}ms: {step['output'][:100]}")

    print(f"\nFinal output: {result['output'][:150]}")
    print(f"Stats: {stats()}")
