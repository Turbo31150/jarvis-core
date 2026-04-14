#!/usr/bin/env python3
"""JARVIS Model Warmup — Pre-warm LLM backends with lightweight inference to reduce cold-start latency"""

import redis
import json
import time
import requests
import threading

r = redis.Redis(decode_responses=True)

WARMUP_KEY = "jarvis:warmup:status"
STATS_KEY = "jarvis:warmup:stats"

WARMUP_PROBES = {
    "m2_qwen35b": {
        "url": "http://192.168.1.26:1234/v1/chat/completions",
        "model": "qwen/qwen3.5-35b-a3b",
        "ollama": False,
    },
    "m32_mistral": {
        "url": "http://192.168.1.113:1234/v1/chat/completions",
        "model": "mistral-7b-instruct-v0.3",
        "ollama": False,
    },
    "m32_phi": {
        "url": "http://192.168.1.113:1234/v1/chat/completions",
        "model": "phi-3.1-mini-128k-instruct",
        "ollama": False,
    },
    "ol1_gemma3": {
        "url": "http://127.0.0.1:11434/api/generate",
        "model": "gemma3:4b",
        "ollama": True,
    },
}

WARMUP_PROMPT = "1"  # minimal prompt to trigger model load


def _probe(name: str, cfg: dict, results: dict):
    t0 = time.perf_counter()
    try:
        if cfg["ollama"]:
            resp = requests.post(
                cfg["url"],
                json={
                    "model": cfg["model"],
                    "prompt": WARMUP_PROMPT,
                    "stream": False,
                    "options": {"num_predict": 1},
                },
                timeout=30,
            )
            ok = resp.status_code == 200
        else:
            resp = requests.post(
                cfg["url"],
                json={
                    "model": cfg["model"],
                    "messages": [{"role": "user", "content": WARMUP_PROMPT}],
                    "max_tokens": 1,
                    "temperature": 0,
                },
                timeout=30,
            )
            ok = resp.status_code == 200 and bool(resp.json().get("choices"))

        lat = round((time.perf_counter() - t0) * 1000)
        results[name] = {"ok": ok, "latency_ms": lat, "cold_start": lat > 3000}
        r.hincrby(STATS_KEY, f"{name}:warmups", 1)
    except Exception as e:
        lat = round((time.perf_counter() - t0) * 1000)
        results[name] = {"ok": False, "latency_ms": lat, "error": str(e)[:60]}
        r.hincrby(STATS_KEY, f"{name}:failures", 1)


def warmup_all(parallel: bool = True) -> dict:
    """Warm up all configured backends."""
    results = {}
    if parallel:
        threads = [
            threading.Thread(target=_probe, args=(n, cfg, results))
            for n, cfg in WARMUP_PROBES.items()
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=35)
    else:
        for name, cfg in WARMUP_PROBES.items():
            _probe(name, cfg, results)

    # Cache results
    r.setex(
        WARMUP_KEY,
        300,
        json.dumps(
            {
                "results": results,
                "ts": time.time(),
                "ok_count": sum(1 for v in results.values() if v.get("ok")),
            }
        ),
    )
    return results


def warmup_backend(name: str) -> dict:
    cfg = WARMUP_PROBES.get(name)
    if not cfg:
        return {"ok": False, "error": f"unknown backend: {name}"}
    results = {}
    _probe(name, cfg, results)
    return results.get(name, {})


def get_status() -> dict:
    raw = r.get(WARMUP_KEY)
    return json.loads(raw) if raw else {"results": {}, "ts": None}


def needs_warmup(name: str, threshold_ms: int = 5000) -> bool:
    """Check if a backend's last warmup was too slow (likely cold)."""
    status = get_status()
    result = status.get("results", {}).get(name, {})
    if not result:
        return True
    age_s = time.time() - status.get("ts", 0)
    return age_s > 600 or result.get("cold_start", False)


def stats() -> dict:
    s = r.hgetall(STATS_KEY)
    status = get_status()
    return {
        "last_warmup": status.get("ts"),
        "backends_ok": status.get("ok_count", 0),
        **{k: int(v) for k, v in s.items()},
    }


if __name__ == "__main__":
    print("Warming up all backends (parallel)...")
    results = warmup_all(parallel=True)
    for name, res in sorted(results.items()):
        icon = "✅" if res["ok"] else "❌"
        cold = " [COLD]" if res.get("cold_start") else ""
        err = f" err={res['error'][:40]}" if res.get("error") else ""
        print(f"  {icon} {name:16s} {res['latency_ms']:5d}ms{cold}{err}")
    ok = sum(1 for v in results.values() if v.get("ok"))
    print(f"\n{ok}/{len(results)} backends warmed | Stats: {stats()}")
