#!/usr/bin/env python3
"""JARVIS Load Tester — Automated load testing for API endpoints"""

import requests
import threading
import time
import json
import statistics
from datetime import datetime

BASE_URL = "http://127.0.0.1:8767"

ENDPOINTS = [
    {"path": "/health",       "method": "GET",  "weight": 10},
    {"path": "/score",        "method": "GET",  "weight": 5},
    {"path": "/mesh",         "method": "GET",  "weight": 3},
    {"path": "/sla",          "method": "GET",  "weight": 2},
    {"path": "/flags",        "method": "GET",  "weight": 2},
    {"path": "/canary",       "method": "GET",  "weight": 1},
    {"path": "/cost/today",   "method": "GET",  "weight": 1},
]


def single_request(endpoint: dict) -> dict:
    url = BASE_URL + endpoint["path"]
    t0 = time.perf_counter()
    try:
        resp = requests.request(endpoint["method"], url, timeout=10)
        latency_ms = round((time.perf_counter() - t0) * 1000)
        return {"ok": resp.status_code < 400, "latency_ms": latency_ms, "status": resp.status_code, "path": endpoint["path"]}
    except Exception as e:
        latency_ms = round((time.perf_counter() - t0) * 1000)
        return {"ok": False, "latency_ms": latency_ms, "error": str(e)[:40], "path": endpoint["path"]}


def run_load_test(duration_s: int = 10, concurrency: int = 5) -> dict:
    results = []
    lock = threading.Lock()
    stop_event = threading.Event()
    t_start = time.time()

    def worker():
        import random
        weights = [e["weight"] for e in ENDPOINTS]
        while not stop_event.is_set():
            endpoint = random.choices(ENDPOINTS, weights=weights, k=1)[0]
            res = single_request(endpoint)
            with lock:
                results.append(res)

    threads = [threading.Thread(target=worker) for _ in range(concurrency)]
    for t in threads:
        t.start()
    time.sleep(duration_s)
    stop_event.set()
    for t in threads:
        t.join(timeout=2)

    # Analyze
    total = len(results)
    ok = sum(1 for r in results if r["ok"])
    latencies = [r["latency_ms"] for r in results if r["ok"]]
    by_path = {}
    for res in results:
        p = res["path"]
        if p not in by_path:
            by_path[p] = {"count": 0, "ok": 0, "latencies": []}
        by_path[p]["count"] += 1
        if res["ok"]:
            by_path[p]["ok"] += 1
            by_path[p]["latencies"].append(res["latency_ms"])

    path_summary = {}
    for p, data in by_path.items():
        lats = data["latencies"]
        path_summary[p] = {
            "count": data["count"],
            "ok_rate_pct": round(data["ok"] / max(data["count"], 1) * 100, 1),
            "avg_ms": sum(lats) // max(len(lats), 1),
            "p99_ms": sorted(lats)[int(len(lats) * 0.99)] if lats else 0,
        }

    return {
        "ts": datetime.now().isoformat()[:19],
        "duration_s": duration_s,
        "concurrency": concurrency,
        "total_requests": total,
        "rps": round(total / duration_s, 1),
        "ok_rate_pct": round(ok / max(total, 1) * 100, 1),
        "avg_ms": sum(latencies) // max(len(latencies), 1) if latencies else 0,
        "p50_ms": statistics.median(latencies) if latencies else 0,
        "p99_ms": sorted(latencies)[int(len(latencies) * 0.99)] if latencies else 0,
        "endpoints": path_summary,
    }


if __name__ == "__main__":
    import sys
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    concurrency = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    print(f"Load test: {duration}s, {concurrency} concurrent workers...")
    res = run_load_test(duration, concurrency)
    print(f"  {res['total_requests']} requests @ {res['rps']} RPS")
    print(f"  OK: {res['ok_rate_pct']}% | avg: {res['avg_ms']}ms | p99: {res['p99_ms']}ms")
    print("  By endpoint:")
    for path, data in sorted(res["endpoints"].items()):
        print(f"    {path}: {data['count']} reqs, {data['ok_rate_pct']}% ok, avg {data['avg_ms']}ms")
