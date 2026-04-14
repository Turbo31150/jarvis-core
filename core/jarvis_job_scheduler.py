#!/usr/bin/env python3
"""JARVIS Job Scheduler — Cron-like job scheduling with Redis persistence"""

import redis
import json
import time
import subprocess
from datetime import datetime

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:jobs"

# Predefined scheduled jobs
SCHEDULED_JOBS = {
    "canary_test":      {"interval_s": 900,  "cmd": ["python3", "/home/turbo/jarvis/core/jarvis_canary_tester.py"]},
    "health_check":     {"interval_s": 60,   "cmd": ["python3", "/home/turbo/jarvis/core/jarvis_health_aggregator.py"]},
    "mesh_scan":        {"interval_s": 30,   "cmd": ["python3", "/home/turbo/jarvis/core/jarvis_service_mesh.py"]},
    "scheduler_tick":   {"interval_s": 30,   "cmd": ["python3", "/home/turbo/jarvis/core/jarvis_adaptive_scheduler.py"]},
    "pattern_detect":   {"interval_s": 300,  "cmd": ["python3", "/home/turbo/jarvis/core/jarvis_pattern_detector.py"]},
    "auto_heal":        {"interval_s": 600,  "cmd": ["python3", "/home/turbo/jarvis/core/jarvis_auto_healer.py"]},
    "data_export":      {"interval_s": 3600, "cmd": ["python3", "/home/turbo/jarvis/core/jarvis_data_exporter.py"]},
    "watchdog":         {"interval_s": 30,   "cmd": ["python3", "/home/turbo/jarvis/core/jarvis_watchdog.py"]},
}


def is_due(job_name: str, interval_s: int) -> bool:
    last_key = f"{PREFIX}:last_run:{job_name}"
    last = r.get(last_key)
    if not last:
        return True
    return time.time() - float(last) >= interval_s


def mark_run(job_name: str):
    r.set(f"{PREFIX}:last_run:{job_name}", time.time())
    r.hincrby(f"{PREFIX}:stats:{job_name}", "runs", 1)


def run_job(job_name: str, job_cfg: dict) -> dict:
    t0 = time.time()
    try:
        result = subprocess.run(job_cfg["cmd"], capture_output=True, text=True, timeout=120)
        duration_ms = round((time.time() - t0) * 1000)
        ok = result.returncode == 0
        r.hincrby(f"{PREFIX}:stats:{job_name}", "ok" if ok else "fail", 1)
        return {"job": job_name, "ok": ok, "duration_ms": duration_ms, "rc": result.returncode}
    except subprocess.TimeoutExpired:
        return {"job": job_name, "ok": False, "error": "timeout"}
    except Exception as e:
        return {"job": job_name, "ok": False, "error": str(e)[:60]}


def tick() -> dict:
    """Check and run all due jobs"""
    ran = []
    skipped = []
    for job_name, cfg in SCHEDULED_JOBS.items():
        if is_due(job_name, cfg["interval_s"]):
            mark_run(job_name)
            result = run_job(job_name, cfg)
            ran.append(result)
        else:
            skipped.append(job_name)
    return {
        "ts": datetime.now().isoformat()[:19],
        "ran": len(ran),
        "skipped": len(skipped),
        "results": ran,
    }


def stats() -> dict:
    result = {}
    for job_name in SCHEDULED_JOBS:
        data = r.hgetall(f"{PREFIX}:stats:{job_name}")
        last_run = r.get(f"{PREFIX}:last_run:{job_name}")
        if data or last_run:
            result[job_name] = {
                "runs": int(data.get("runs", 0)),
                "ok": int(data.get("ok", 0)),
                "fail": int(data.get("fail", 0)),
                "last_run_ago_s": round(time.time() - float(last_run)) if last_run else None,
            }
    return result


def reset_job(job_name: str):
    r.delete(f"{PREFIX}:last_run:{job_name}")


if __name__ == "__main__":
    import sys
    if "--run" in sys.argv:
        job = sys.argv[sys.argv.index("--run") + 1]
        if job in SCHEDULED_JOBS:
            reset_job(job)
            res = run_job(job, SCHEDULED_JOBS[job])
            print(f"Ran {job}: {res}")
        else:
            print(f"Unknown job: {job}")
    else:
        s = stats()
        print(f"Scheduled jobs: {len(SCHEDULED_JOBS)}")
        for name, data in s.items():
            ago = f"{data['last_run_ago_s']}s ago" if data.get("last_run_ago_s") is not None else "never"
            print(f"  {name}: {data['runs']} runs, last {ago}")
        if not s:
            print("  No runs recorded yet")
