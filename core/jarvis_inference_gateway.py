#!/usr/bin/env python3
"""JARVIS Inference Gateway — Unified LLM inference with fallback, cache, and metrics"""

import redis
import requests
import json
import time
from datetime import datetime

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:infer"


def infer(
    prompt: str,
    task_type: str = "default",
    use_cache: bool = True,
    max_tokens: int = 500,
    temperature: float = 0.1,
) -> dict:
    t_start = time.perf_counter()
    request_id = f"req_{int(time.time()*1000)}"

    # 1. Check cache
    if use_cache:
        try:
            from jarvis_llm_cache import get as cache_get, set_cache
            cached = cache_get(prompt, task_type=task_type)
            if cached:
                return {"ok": True, "response": cached, "source": "cache", "ms": 0, "request_id": request_id}
        except ImportError:
            pass

    # 2. Select best model
    try:
        from jarvis_model_selector import select
        model_info = select(task_type)
    except Exception:
        model_info = {"url": "http://127.0.0.1:11434", "model": "gemma3:4b", "api": "ollama", "model_key": "ol1_gemma3"}

    if "error" in model_info:
        return {"ok": False, "error": model_info["error"], "request_id": request_id}

    # 3. Call model
    url = model_info["url"]
    model = model_info["model"]
    api_type = model_info.get("api", "openai")

    try:
        if api_type == "ollama":
            resp = requests.post(f"{url}/api/generate", json={
                "model": model, "prompt": prompt, "stream": False,
                "options": {"num_predict": max_tokens, "temperature": temperature},
            }, timeout=30)
            response_text = resp.json().get("response", "")
        else:
            resp = requests.post(f"{url}/v1/chat/completions", json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": temperature,
            }, timeout=30)
            msg = resp.json()["choices"][0]["message"]
            response_text = msg.get("content") or msg.get("reasoning_content", "")

        ms = round((time.perf_counter() - t_start) * 1000)

        # 4. Store in cache
        if use_cache and response_text:
            try:
                from jarvis_llm_cache import set_cache
                set_cache(prompt, response_text, model_key=model_info.get("model_key", ""), task_type=task_type)
            except Exception:
                pass

        # 5. Track metrics
        r.hincrby(f"{PREFIX}:stats:{model_info.get('model_key','unknown')}", "calls", 1)
        r.hincrbyfloat(f"{PREFIX}:stats:{model_info.get('model_key','unknown')}", "total_ms", ms)

        return {
            "ok": True,
            "response": response_text,
            "model": model,
            "node": model_info.get("node", "?"),
            "model_key": model_info.get("model_key", "?"),
            "ms": ms,
            "source": "live",
            "request_id": request_id,
        }

    except Exception as e:
        ms = round((time.perf_counter() - t_start) * 1000)
        # Fallback to OL1
        try:
            resp = requests.post("http://127.0.0.1:11434/api/generate", json={
                "model": "gemma3:4b", "prompt": prompt, "stream": False, "options": {"num_predict": max_tokens}
            }, timeout=20)
            response_text = resp.json().get("response", "")
            ms2 = round((time.perf_counter() - t_start) * 1000)
            return {"ok": True, "response": response_text, "model": "gemma3:4b", "node": "OL1", "ms": ms2, "source": "fallback", "request_id": request_id}
        except Exception as e2:
            return {"ok": False, "error": f"{str(e)[:40]} | fallback: {str(e2)[:40]}", "ms": ms, "request_id": request_id}


def stats() -> dict:
    result = {}
    for key in r.scan_iter(f"{PREFIX}:stats:*"):
        model_key = key.replace(f"{PREFIX}:stats:", "")
        data = r.hgetall(key)
        calls = int(data.get("calls", 0))
        total_ms = float(data.get("total_ms", 0))
        result[model_key] = {"calls": calls, "avg_ms": round(total_ms/max(calls,1))}
    return result


if __name__ == "__main__":
    print("Testing inference gateway...")
    res = infer("What is 2+2? Answer with just the number.", task_type="fast")
    icon = "✅" if res["ok"] else "❌"
    print(f"  {icon} [{res.get('node','?')}/{res.get('model_key','?')}] {res.get('ms')}ms source={res.get('source')}")
    if res["ok"]:
        print(f"  Response: {res['response'][:80]}")
    # Second call — should hit cache
    res2 = infer("What is 2+2? Answer with just the number.", task_type="fast")
    print(f"  Cache hit: {res2.get('source') == 'cache'}")
    print(f"Stats: {stats()}")
