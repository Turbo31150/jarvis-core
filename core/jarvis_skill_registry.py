#!/usr/bin/env python3
"""JARVIS Skill Registry — Register, discover, and invoke agent skills dynamically"""

import redis
import json
import time
import importlib

r = redis.Redis(decode_responses=True)

REGISTRY_KEY = "jarvis:skills:registry"
INVOKE_LOG_KEY = "jarvis:skills:invoke_log"
STATS_KEY = "jarvis:skills:stats"

# Built-in skills
BUILTIN_SKILLS = [
    {
        "name": "ask_llm",
        "module": "jarvis_llm_router",
        "func": "ask",
        "desc": "Ask a question to the best LLM",
        "params": ["prompt", "task_type"],
    },
    {
        "name": "rag_query",
        "module": "jarvis_rag_engine",
        "func": "ask",
        "desc": "RAG-enhanced query over knowledge base",
        "params": ["query"],
    },
    {
        "name": "classify_intent",
        "module": "jarvis_intent_router",
        "func": "classify",
        "desc": "Classify user intent",
        "params": ["text"],
    },
    {
        "name": "rerank",
        "module": "jarvis_reranker",
        "func": "rerank",
        "desc": "Rerank list of results",
        "params": ["items", "query"],
    },
    {
        "name": "optimize_prompt",
        "module": "jarvis_token_optimizer",
        "func": "optimize",
        "desc": "Optimize prompt to reduce tokens",
        "params": ["prompt"],
    },
    {
        "name": "kb_search",
        "module": "jarvis_knowledge_base",
        "func": "search",
        "desc": "Search knowledge base",
        "params": ["query"],
    },
    {
        "name": "cluster_health",
        "module": "jarvis_health_scorer",
        "func": "stats",
        "desc": "Get cluster health score",
        "params": [],
    },
    {
        "name": "gpu_status",
        "module": "jarvis_gpu_balancer",
        "func": "get_gpu_load",
        "desc": "Get GPU load and temperatures",
        "params": [],
    },
    {
        "name": "emit_signal",
        "module": "jarvis_signal_bus",
        "func": "emit",
        "desc": "Emit a signal on the bus",
        "params": ["signal_type", "source", "payload"],
    },
    {
        "name": "run_pipeline",
        "module": "jarvis_pipeline_builder",
        "func": "run_pipeline",
        "desc": "Execute a defined pipeline",
        "params": ["name", "inputs"],
    },
    {
        "name": "snapshot",
        "module": "jarvis_snapshot",
        "func": "capture",
        "desc": "Take a system state snapshot",
        "params": ["label"],
    },
    {
        "name": "multi_query",
        "module": "jarvis_multi_query",
        "func": "consensus",
        "desc": "Fan-out query to multiple LLMs",
        "params": ["prompt"],
    },
]


def bootstrap():
    """Load built-in skills into registry."""
    for skill in BUILTIN_SKILLS:
        r.hset(
            REGISTRY_KEY,
            skill["name"],
            json.dumps(
                {
                    **skill,
                    "registered_at": time.time(),
                    "builtin": True,
                }
            ),
        )
    return len(BUILTIN_SKILLS)


def register(
    name: str,
    module: str,
    func: str,
    desc: str = "",
    params: list = None,
    tags: list = None,
) -> dict:
    """Register a custom skill."""
    skill = {
        "name": name,
        "module": module,
        "func": func,
        "desc": desc,
        "params": params or [],
        "tags": tags or [],
        "registered_at": time.time(),
        "builtin": False,
    }
    r.hset(REGISTRY_KEY, name, json.dumps(skill))
    return skill


def invoke(name: str, *args, **kwargs) -> dict:
    """Invoke a registered skill by name."""
    raw = r.hget(REGISTRY_KEY, name)
    if not raw:
        return {"ok": False, "error": f"skill not found: {name}"}

    skill = json.loads(raw)
    t0 = time.perf_counter()
    try:
        mod = importlib.import_module(skill["module"])
        fn = getattr(mod, skill["func"])
        result = fn(*args, **kwargs)
        lat = round((time.perf_counter() - t0) * 1000)
        r.hincrby(STATS_KEY, f"{name}:ok", 1)
        r.lpush(
            INVOKE_LOG_KEY,
            json.dumps({"skill": name, "ok": True, "lat_ms": lat, "ts": time.time()}),
        )
        r.ltrim(INVOKE_LOG_KEY, 0, 499)
        return {"ok": True, "result": result, "latency_ms": lat}
    except Exception as e:
        lat = round((time.perf_counter() - t0) * 1000)
        r.hincrby(STATS_KEY, f"{name}:err", 1)
        return {"ok": False, "error": str(e)[:100], "latency_ms": lat}


def list_skills(tag: str = None) -> list:
    all_skills = [json.loads(v) for v in r.hvals(REGISTRY_KEY)]
    if tag:
        all_skills = [s for s in all_skills if tag in s.get("tags", [])]
    return sorted(all_skills, key=lambda x: x["name"])


def get(name: str) -> dict | None:
    raw = r.hget(REGISTRY_KEY, name)
    return json.loads(raw) if raw else None


def stats() -> dict:
    s = r.hgetall(STATS_KEY)
    total_skills = r.hlen(REGISTRY_KEY)
    ok_calls = sum(int(v) for k, v in s.items() if k.endswith(":ok"))
    err_calls = sum(int(v) for k, v in s.items() if k.endswith(":err"))
    return {"total_skills": total_skills, "ok_calls": ok_calls, "err_calls": err_calls}


if __name__ == "__main__":
    n = bootstrap()
    print(f"Bootstrapped {n} built-in skills")

    # Register a custom skill
    register(
        "jarvis_status",
        "jarvis_health_scorer",
        "get",
        desc="Get current JARVIS health",
        tags=["monitoring"],
    )

    skills = list_skills()
    print(f"Total skills: {len(skills)}")
    for s in skills[:5]:
        print(f"  {s['name']:20s} — {s['desc'][:50]}")

    # Invoke a skill
    result = invoke("classify_intent", "What is the GPU temperature?")
    if result["ok"]:
        print(
            f"\nclassify_intent: {result['result']['intent']} ({result['latency_ms']}ms)"
        )

    print(f"\nStats: {stats()}")
