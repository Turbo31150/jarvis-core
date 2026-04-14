#!/usr/bin/env python3
"""JARVIS Workflow Engine — Execute sequential and parallel agent workflows"""
import redis, json, time, concurrent.futures
from datetime import datetime

r = redis.Redis(decode_responses=True)

# Predefined workflows
WORKFLOWS = {
    "morning_check": {
        "steps": [
            {"name": "gpu_health",    "cmd": "python3 /home/turbo/jarvis/core/jarvis_gpu_health.py"},
            {"name": "canary_tests",  "cmd": "python3 /home/turbo/jarvis/core/jarvis_canary_tester.py"},
            {"name": "node_status",   "cmd": "python3 /home/turbo/jarvis/core/jarvis_node_rebalancer.py"},
            {"name": "predictions",   "cmd": "python3 /home/turbo/jarvis/core/jarvis_health_predictor.py"},
        ],
        "parallel": True,
        "on_fail": "alert"
    },
    "backup_and_report": {
        "steps": [
            {"name": "backup",        "cmd": "python3 /home/turbo/jarvis/core/jarvis_backup_manager.py"},
            {"name": "cost_report",   "cmd": "python3 /home/turbo/jarvis/core/jarvis_cost_reporter.py"},
            {"name": "consolidate",   "cmd": "python3 /home/turbo/jarvis/core/jarvis_memory_consolidator.py"},
        ],
        "parallel": False,
        "on_fail": "continue"
    },
    "full_health": {
        "steps": [
            {"name": "canary",        "cmd": "python3 /home/turbo/jarvis/core/jarvis_canary_tester.py"},
            {"name": "gpu_health",    "cmd": "python3 /home/turbo/jarvis/core/jarvis_gpu_health.py"},
            {"name": "predict",       "cmd": "python3 /home/turbo/jarvis/core/jarvis_health_predictor.py"},
            {"name": "sla",           "cmd": "python3 /home/turbo/jarvis/core/jarvis_sla_tracker.py"},
        ],
        "parallel": True,
        "on_fail": "alert"
    }
}

def run_step(step: dict) -> dict:
    import subprocess
    t0 = time.perf_counter()
    try:
        out = subprocess.run(step["cmd"], shell=True, capture_output=True, text=True, timeout=30)
        return {"name": step["name"], "ok": out.returncode == 0,
                "output": (out.stdout or out.stderr)[:200],
                "duration_ms": round((time.perf_counter()-t0)*1000)}
    except Exception as e:
        return {"name": step["name"], "ok": False, "output": str(e)[:100],
                "duration_ms": round((time.perf_counter()-t0)*1000)}

def run_workflow(name: str) -> dict:
    wf = WORKFLOWS.get(name)
    if not wf:
        return {"error": f"unknown workflow: {name}"}
    
    ts = datetime.now().isoformat()[:19]
    results = []
    
    if wf["parallel"]:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            futures = {ex.submit(run_step, s): s for s in wf["steps"]}
            for fut in concurrent.futures.as_completed(futures):
                results.append(fut.result())
    else:
        for step in wf["steps"]:
            res = run_step(step)
            results.append(res)
            if not res["ok"] and wf["on_fail"] == "stop":
                break
    
    passed = sum(1 for r2 in results if r2["ok"])
    summary = {"workflow": name, "ts": ts, "passed": passed,
               "total": len(results), "steps": results}
    
    r.lpush("jarvis:workflow_runs", json.dumps(summary))
    r.ltrim("jarvis:workflow_runs", 0, 49)
    return summary

if __name__ == "__main__":
    print("Running 'morning_check' workflow (parallel)...")
    res = run_workflow("morning_check")
    print(f"  {res['passed']}/{res['total']} steps OK")
    for s in res["steps"]:
        print(f"  {'✅' if s['ok'] else '❌'} {s['name']}: {s['duration_ms']}ms")
