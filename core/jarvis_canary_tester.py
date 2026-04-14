#!/usr/bin/env python3
"""JARVIS Canary Tester — Automated regression tests after deployments"""
import requests, redis, json, time, sqlite3
from datetime import datetime

r = redis.Redis(decode_responses=True)
DB = "/home/turbo/jarvis/core/jarvis_master_index.db"

TESTS = [
    ("redis_ping",        lambda: r.ping()),
    ("redis_score",       lambda: bool(r.get("jarvis:score"))),
    ("api_health",        lambda: requests.get("http://localhost:8767/health", timeout=3).ok),
    ("api_score",         lambda: requests.get("http://localhost:8767/score", timeout=3).ok),
    ("dashboard_up",      lambda: requests.get("http://localhost:8765/", timeout=3).ok),
    ("sqlite_accessible", lambda: bool(sqlite3.connect(DB).execute("SELECT 1").fetchone())),
    ("gpu_data_fresh",    lambda: bool(r.get("jarvis:gpu:0:temp"))),
]

def run_all() -> dict:
    results = {"ts": datetime.now().isoformat()[:19], "passed": 0, "failed": 0, "tests": {}}
    for name, fn in TESTS:
        try:
            ok = bool(fn())
        except Exception as e:
            ok = False
            results["tests"][name] = {"ok": False, "error": str(e)[:80]}
            results["failed"] += 1
            continue
        results["tests"][name] = {"ok": ok}
        if ok:
            results["passed"] += 1
        else:
            results["failed"] += 1
    
    total = results["passed"] + results["failed"]
    results["score_pct"] = round(results["passed"] / max(total, 1) * 100)
    r.setex("jarvis:canary:last", 3600, json.dumps(results))
    
    if results["failed"] > 0:
        r.publish("jarvis:events", json.dumps({
            "type": "canary_failure", "severity": "critical",
            "data": {"failed": results["failed"], "passed": results["passed"]},
            "ts": results["ts"]
        }))
    return results

if __name__ == "__main__":
    results = run_all()
    print(f"Canary: {results['passed']}/{results['passed']+results['failed']} passed ({results['score_pct']}%)")
    for name, r2 in results["tests"].items():
        status = "✅" if r2["ok"] else "❌"
        print(f"  {status} {name}" + (f": {r2.get('error','')}" if not r2["ok"] else ""))
