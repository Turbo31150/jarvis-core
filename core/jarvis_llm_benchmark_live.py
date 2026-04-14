#!/usr/bin/env python3
"""JARVIS LLM Live Benchmark — Real-time latency test on available models"""
import requests, redis, json, time, concurrent.futures
from datetime import datetime

r = redis.Redis(decode_responses=True)

BENCH_PROMPT = "What is 42? Reply in one word."

ENDPOINTS = [
    ("m2_35b",  "http://192.168.1.26:1234", "qwen/qwen3.5-35b-a3b",           "lmstudio", 45),
    ("m2_r1",   "http://192.168.1.26:1234", "deepseek/deepseek-r1-0528-qwen3-8b", "lmstudio", 60),
    ("ol1_gemma","http://127.0.0.1:11434",  "gemma3:4b",                       "ollama",   30),
]

def bench_one(name, host, model, backend, timeout) -> dict:
    t0 = time.perf_counter()
    try:
        if backend == "ollama":
            resp = requests.post(f"{host}/api/generate",
                json={"model": model, "prompt": BENCH_PROMPT, "stream": False,
                      "options": {"num_predict": 10}}, timeout=timeout)
            result = resp.json().get("response", "")
        else:
            resp = requests.post(f"{host}/v1/chat/completions",
                json={"model": model, "messages": [{"role":"user","content":BENCH_PROMPT}],
                      "max_tokens": 10, "temperature": 0}, timeout=timeout)
            msg = resp.json()["choices"][0]["message"]
            result = msg.get("content") or msg.get("reasoning_content", "")
        lat = round((time.perf_counter() - t0) * 1000)
        ok = bool(result)
        r.setex(f"jarvis:bench_live:{name}:latency_ms", 300, lat)
        r.setex(f"jarvis:bench_live:{name}:ok", 300, "1" if ok else "0")
        return {"name": name, "latency_ms": lat, "ok": ok, "result": result[:20]}
    except Exception as e:
        r.setex(f"jarvis:bench_live:{name}:ok", 300, "0")
        return {"name": name, "latency_ms": -1, "ok": False, "error": str(e)[:60]}

def run_parallel() -> list:
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        futures = [ex.submit(bench_one, *ep) for ep in ENDPOINTS]
        return [f.result() for f in concurrent.futures.as_completed(futures)]

if __name__ == "__main__":
    print(f"Benchmarking {len(ENDPOINTS)} LLM endpoints...")
    results = run_parallel()
    for r2 in sorted(results, key=lambda x: x.get("latency_ms", 9999)):
        icon = "✅" if r2["ok"] else "❌"
        lat = r2["latency_ms"]
        print(f"  {icon} {r2['name']}: {lat}ms — {r2.get('result','')[:20]}")
