#!/usr/bin/env python3
"""JARVIS LLM Monitor — tokens/s, latence, erreurs par modèle → Redis"""

import time
import requests
import redis
import json
from datetime import datetime

r = redis.Redis(decode_responses=True)

BACKENDS = {
    # qwen9b skipped: thinking bug (blocks indefinitely even with enable_thinking:false)
    "m2-r1": ("http://192.168.1.26:1234", "deepseek/deepseek-r1-0528-qwen3-8b"),
    "m2-35b": ("http://192.168.1.26:1234", "qwen/qwen3.5-35b-a3b"),
    "ol1-gemma3": (None, "gemma3:4b"),  # ollama
    "ol1-qwen17": (None, "qwen3:1.7b"),
}
PROMPT = "Réponds 'ok' en un mot."


def bench_lmstudio(host, model):
    t0 = time.perf_counter()
    try:
        resp = requests.post(
            f"{host}/v1/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": PROMPT}],
                "max_tokens": 5,
                "temperature": 0,
                "chat_template_kwargs": {"enable_thinking": False},
            },
            timeout=20,
        )
        d = resp.json()
        msg = d["choices"][0]["message"]
        content = msg.get("content") or msg.get("reasoning_content", "")
        usage = d.get("usage", {})
        elapsed = time.perf_counter() - t0
        tps = usage.get("completion_tokens", 1) / elapsed if elapsed > 0 else 0
        return {
            "ok": bool(content),
            "latency_ms": round(elapsed * 1000),
            "tokens_s": round(tps, 1),
            "error": None,
        }
    except Exception as e:
        return {"ok": False, "latency_ms": -1, "tokens_s": 0, "error": str(e)[:80]}


def bench_ollama(model):
    t0 = time.perf_counter()
    try:
        resp = requests.post(
            "http://127.0.0.1:11434/api/generate",
            json={
                "model": model,
                "prompt": PROMPT,
                "stream": False,
                "options": {"num_predict": 5},
            },
            timeout=20,
        )
        d = resp.json()
        elapsed = time.perf_counter() - t0
        tps = d.get("eval_count", 1) / (d.get("eval_duration", 1) / 1e9)
        return {
            "ok": bool(d.get("response")),
            "latency_ms": round(elapsed * 1000),
            "tokens_s": round(tps, 1),
            "error": None,
        }
    except Exception as e:
        return {"ok": False, "latency_ms": -1, "tokens_s": 0, "error": str(e)[:80]}


def run_once():
    ts = datetime.now().isoformat()
    results = {}
    for name, (host, model) in BACKENDS.items():
        if host:
            results[name] = bench_lmstudio(host, model)
        else:
            results[name] = bench_ollama(model)
        results[name]["model"] = model
        results[name]["ts"] = ts
        # Store in Redis
        r.setex(f"jarvis:llm:{name}", 300, json.dumps(results[name]))
        status = "✅" if results[name]["ok"] else "❌"
        print(
            f"  {status} {name}: {results[name]['latency_ms']}ms {results[name]['tokens_s']}tok/s"
        )
    r.setex("jarvis:llm:summary", 300, json.dumps({"ts": ts, "backends": results}))
    return results


if __name__ == "__main__":
    print(f"[{datetime.now().strftime('%H:%M:%S')}] LLM Monitor run")
    run_once()
    print("Done — saved to Redis jarvis:llm:*")
