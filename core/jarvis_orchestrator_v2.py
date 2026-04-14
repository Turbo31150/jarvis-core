#!/usr/bin/env python3
"""JARVIS Orchestrator v2 — Unified entry point orchestrating all JARVIS subsystems"""

import redis
import json
import time
from datetime import datetime

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:orch"

VERSION = "2.0.0"

SUBSYSTEMS = {
    "core":     ["circuit_breaker", "rate_limiter", "feature_flags", "config_manager"],
    "llm":      ["llm_router", "llm_cache", "token_counter", "feedback_loop"],
    "queue":    ["task_queue", "task_priority_queue", "task_dispatcher"],
    "health":   ["health_aggregator", "canary_tester", "service_mesh", "watchdog"],
    "data":     ["context_cache", "embedding_cache", "data_exporter"],
    "ops":      ["auto_healer", "adaptive_scheduler", "job_scheduler", "runbook"],
    "observe":  ["sla_tracker", "metrics_aggregator", "request_logger", "trace_collector"],
    "infra":    ["plugin_loader", "event_bus", "pipeline_executor", "notification_router"],
}


def startup_check() -> dict:
    """Verify all critical subsystems are operational"""
    results = {}
    critical = ["circuit_breaker", "rate_limiter", "llm_router", "task_queue"]
    for module_name in critical:
        try:
            import importlib, sys
            sys.path.insert(0, "/home/turbo/jarvis/core")
            importlib.import_module(f"jarvis_{module_name}")
            results[module_name] = "ok"
        except Exception as e:
            results[module_name] = f"error: {str(e)[:40]}"

    ok_count = sum(1 for v in results.values() if v == "ok")
    return {
        "version": VERSION,
        "ts": datetime.now().isoformat()[:19],
        "critical_ok": ok_count,
        "critical_total": len(critical),
        "ready": ok_count == len(critical),
        "modules": results,
    }


def full_status() -> dict:
    """Comprehensive system status"""
    score = json.loads(r.get("jarvis:score") or "{}")
    canary = json.loads(r.get("jarvis:canary:last") or "{}")
    mesh = json.loads(r.get("jarvis:mesh:summary") or "{}")
    health = json.loads(r.get("jarvis:health:last") or "{}")

    # Count total modules
    import glob
    total_modules = len(glob.glob("/home/turbo/jarvis/core/jarvis_*.py"))

    return {
        "version": VERSION,
        "ts": datetime.now().isoformat()[:19],
        "score": score.get("total", 0),
        "canary": f"{canary.get('passed', 0)}/{canary.get('passed', 0) + canary.get('failed', 0)}",
        "services": f"{mesh.get('up', 0)}/{mesh.get('total', 0)}",
        "overall_health": health.get("overall", "unknown"),
        "total_modules": total_modules,
        "subsystems": len(SUBSYSTEMS),
        "subsystem_modules": sum(len(v) for v in SUBSYSTEMS.values()),
        "nodes": {n: r.get(f"jarvis:node:{n}:status") or "unknown" for n in ["M1", "M2", "M3", "OL1"]},
    }


def dispatch(task: dict) -> dict:
    """Unified task dispatch to the right subsystem"""
    task_type = task.get("type", "generic")
    payload = task.get("payload", {})

    # Route to appropriate handler
    if task_type in ("llm", "ask", "code", "classify"):
        try:
            from jarvis_llm_router import ask
            result = ask(payload.get("prompt", ""), task_type)
            return {"ok": True, "result": result, "handler": "llm_router"}
        except Exception as e:
            return {"ok": False, "error": str(e), "handler": "llm_router"}

    elif task_type == "health_check":
        try:
            from jarvis_health_aggregator import aggregate
            return {"ok": True, "result": aggregate(), "handler": "health_aggregator"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    elif task_type == "runbook":
        try:
            from jarvis_runbook import run_runbook
            return {"ok": True, "result": run_runbook(payload.get("name", "full_health_check")), "handler": "runbook"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    else:
        # Generic queue
        try:
            from jarvis_task_dispatcher import dispatch as _dispatch
            tid = _dispatch(task_type, payload)
            return {"ok": True, "task_id": tid, "handler": "task_dispatcher"}
        except Exception as e:
            return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    print(f"JARVIS Orchestrator v{VERSION}")
    check = startup_check()
    status_icon = "✅" if check["ready"] else "⚠️"
    print(f"{status_icon} Critical modules: {check['critical_ok']}/{check['critical_total']}")

    status = full_status()
    print(f"Score: {status['score']}/100 | Canary: {status['canary']} | Services: {status['services']}")
    print(f"Total modules: {status['total_modules']} | Health: {status['overall_health']}")
    print(f"Nodes: {status['nodes']}")
