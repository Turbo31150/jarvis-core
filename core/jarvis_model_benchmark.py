#!/usr/bin/env python3
"""JARVIS Model Benchmark — Latency/quality benchmarks across all LLM backends"""

import redis
import requests
import time
import json
from datetime import datetime

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:bench"

BENCHMARKS = [
    {"id": "math_simple", "prompt": "2+2=?", "expected_contains": "4", "type": "fast"},
    {"id": "code_simple", "prompt": "Write a Python function to reverse a string", "type": "code"},
    {"id": "reasoning", "prompt": "If all roses are flowers, and some flowers fade quickly, can we conclude all roses fade quickly?", "type": "reasoning"},
    {"id": "summary", "prompt": "Summarize in one sentence: Redis is an in-memory data structure store used as database, cache, message broker, and streaming engine.", "type": "summary"},
]

BACKENDS = {
    "m2_35b": {
        "url": "http://192.168.1.26:1234/v1/chat/completions",
        "model": "qwen/qwen3.5-35b-a3b",
        "type": "openai",
        "timeout": 45,
    },
    "m2_r1": {
        "url": "http://192.168.1.26:1234/v1/chat/completions",
        "model": "deepseek/deepseek-r1-0528-qwen3-8b",
        "type": "openai",
        "timeout": 60,
    },
    "ol1_gemma3": {
        "url": "http://127.0.0.1:11434/api/generate",
        "model": "gemma3:4b",
        "type": "ollama",
        "timeout": 30,
    },
}


def run_single(backend_name: str, bench: dict) -> dict:
    cfg = BACKENDS[backend_name]
    t0 = time.perf_counter()
    try:
        if cfg["type"] == "openai":
            resp = requests.post(cfg["url"], json={
                "model": cfg["model"],
                "messages": [{"role": "user", "content": bench["prompt"]}],
                "max_tokens": 200,
                "temperature": 0.1,
            }, timeout=cfg["timeout"])
            msg = resp.json()["choices"][0]["message"]
            text = msg.get("content") or msg.get("reasoning_content", "")
        else:
            resp = requests.post(cfg["url"], json={
                "model": cfg["model"],
                "prompt": bench["prompt"],
                "stream": False,
                "options": {"num_predict": 200},
            }, timeout=cfg["timeout"])
            text = resp.json().get("response", "")
        lat = round((time.perf_counter() - t0) * 1000)
        correct = bench.get("expected_contains", "").lower() in text.lower() if bench.get("expected_contains") else None
        return {"latency_ms": lat, "ok": True, "correct": correct, "response_len": len(text)}
    except Exception as e:
        lat = round((time.perf_counter() - t0) * 1000)
        return {"latency_ms": lat, "ok": False, "error": str(e)[:60]}


def run_benchmark(backend_name: str = None, bench_id: str = None) -> dict:
    backends = [backend_name] if backend_name else list(BACKENDS.keys())
    benches = [b for b in BENCHMARKS if not bench_id or b["id"] == bench_id]
    results = {}
    for bname in backends:
        results[bname] = {}
        for bench in benches:
            res = run_single(bname, bench)
            results[bname][bench["id"]] = res
            key = f"{PREFIX}:{bname}:{bench['id']}"
            r.hset(key, mapping={"latency_ms": res["latency_ms"], "ok": str(res["ok"]), "ts": time.strftime("%H:%M:%S")})
            r.expire(key, 3600)
    # Summary
    summary = {}
    for bname, bench_results in results.items():
        ok_count = sum(1 for v in bench_results.values() if v.get("ok"))
        avg_lat = sum(v["latency_ms"] for v in bench_results.values()) // max(len(bench_results), 1)
        summary[bname] = {"ok": ok_count, "total": len(bench_results), "avg_ms": avg_lat}
    r.setex(f"{PREFIX}:last", 3600, json.dumps({"ts": datetime.now().isoformat()[:19], "summary": summary}))
    return {"results": results, "summary": summary}


def get_fastest_backend(task_type: str = "fast") -> str:
    """Return backend with lowest average latency from recent benchmarks"""
    best = None
    best_lat = 999999
    for bname in BACKENDS:
        lats = []
        for bench in BENCHMARKS:
            lat = r.hget(f"{PREFIX}:{bname}:{bench['id']}", "latency_ms")
            if lat:
                lats.append(int(lat))
        if lats:
            avg = sum(lats) // len(lats)
            if avg < best_lat:
                best_lat = avg
                best = bname
    return best or "ol1_gemma3"


if __name__ == "__main__":
    import sys
    backend = sys.argv[1] if len(sys.argv) > 1 else None
    bench = sys.argv[2] if len(sys.argv) > 2 else "math_simple"
    print(f"Benchmarking {'all' if not backend else backend} on '{bench}'...")
    res = run_benchmark(backend, bench)
    for bname, s in res["summary"].items():
        print(f"  {bname}: {s['ok']}/{s['total']} ok, avg {s['avg_ms']}ms")
