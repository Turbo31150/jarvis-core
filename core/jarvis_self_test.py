#!/usr/bin/env python3
"""JARVIS Self Test — Import and smoke-test all core modules"""

import importlib
import sys
import os
import redis
from datetime import datetime

r = redis.Redis(decode_responses=True)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MODULES = [
    ("jarvis_circuit_breaker", "status_all"),
    ("jarvis_rate_limiter", "stats"),
    ("jarvis_context_cache", "stats"),
    ("jarvis_llm_router", "stats"),
    ("jarvis_task_queue", "stats"),
    ("jarvis_feature_flags", "all_flags"),
    ("jarvis_intent_classifier", "classify", "hello test"),
    ("jarvis_alert_aggregator", "stats"),
    ("jarvis_sla_tracker", "full_report"),
    ("jarvis_gpu_balancer", "get_gpu_load"),
    ("jarvis_model_registry", "get_best_for", "code"),
    ("jarvis_knowledge_graph", "build_graph"),
]


def run() -> dict:
    results = {
        "ts": datetime.now().isoformat()[:19],
        "passed": 0,
        "failed": 0,
        "modules": {},
    }
    for item in MODULES:
        module_name, func_name = item[0], item[1]
        test_args = [item[2]] if len(item) > 2 else []
        try:
            mod = importlib.import_module(module_name)
            fn = getattr(mod, func_name)
            fn(*test_args) if test_args else fn()  # smoke test
            results["modules"][module_name] = "ok"
            results["passed"] += 1
        except Exception as e:
            results["modules"][module_name] = f"FAIL: {str(e)[:60]}"
            results["failed"] += 1

    results["score_pct"] = round(results["passed"] / max(len(MODULES), 1) * 100)
    r.setex("jarvis:self_test:last", 3600, __import__("json").dumps(results))
    return results


if __name__ == "__main__":
    print(f"Self-testing {len(MODULES)} modules...")
    res = run()
    print(f"Result: {res['passed']}/{len(MODULES)} passed ({res['score_pct']}%)")
    for mod, status in res["modules"].items():
        icon = "✅" if status == "ok" else "❌"
        print(f"  {icon} {mod.replace('jarvis_', '')}: {status}")
