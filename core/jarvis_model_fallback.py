#!/usr/bin/env python3
"""JARVIS Model Fallback — Cascading fallback chain with automatic failover between backends"""

import redis
import time
import requests

r = redis.Redis(decode_responses=True)

STATS_KEY = "jarvis:fallback:stats"

# Fallback chains per task type
CHAINS = {
    "code": [
        ("m2", "http://192.168.1.26:1234", "qwen/qwen3.5-35b-a3b", 45),
        ("m32", "http://192.168.1.113:1234", "mistral-7b-instruct-v0.3", 30),
        ("ol1", "http://127.0.0.1:11434", "gemma3:4b", 20),
    ],
    "reasoning": [
        ("m2", "http://192.168.1.26:1234", "deepseek/deepseek-r1-0528-qwen3-8b", 60),
        ("m32", "http://192.168.1.113:1234", "deepseek/deepseek-r1-0528-qwen3-8b", 60),
        ("ol1", "http://127.0.0.1:11434", "gemma3:4b", 20),
    ],
    "fast": [
        ("ol1", "http://127.0.0.1:11434", "gemma3:4b", 10),
        ("m32", "http://192.168.1.113:1234", "phi-3.1-mini-128k-instruct", 15),
        ("m2", "http://192.168.1.26:1234", "qwen/qwen3-8b", 20),
    ],
    "default": [
        ("m2", "http://192.168.1.26:1234", "qwen/qwen3.5-35b-a3b", 45),
        ("m32", "http://192.168.1.113:1234", "mistral-7b-instruct-v0.3", 30),
        ("ol1", "http://127.0.0.1:11434", "gemma3:4b", 20),
    ],
}


def _call(host: str, model: str, prompt: str, timeout: int) -> str:
    if "11434" in host:
        resp = requests.post(
            f"{host}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {"num_predict": 500},
            },
            timeout=timeout,
        )
        return resp.json().get("response", "")
    else:
        resp = requests.post(
            f"{host}/v1/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 500,
                "temperature": 0.1,
            },
            timeout=timeout,
        )
        choices = resp.json().get("choices", [])
        return choices[0]["message"].get("content", "") if choices else ""


def ask(prompt: str, task_type: str = "default") -> dict:
    """Try backends in fallback chain until one succeeds."""
    chain = CHAINS.get(task_type, CHAINS["default"])
    attempts = []

    for backend, host, model, timeout in chain:
        # Check circuit breaker
        state = r.hget(f"jarvis:cb:{backend}", "state") or "closed"
        if state == "open":
            attempts.append(
                {"backend": backend, "skipped": True, "reason": "circuit_open"}
            )
            continue

        t0 = time.perf_counter()
        try:
            result = _call(host, model, prompt, timeout)
            lat = round((time.perf_counter() - t0) * 1000)
            if result:
                r.hincrby(STATS_KEY, f"{backend}:success", 1)
                r.hincrby(STATS_KEY, f"{task_type}:success", 1)
                return {
                    "text": result,
                    "backend": backend,
                    "model": model,
                    "latency_ms": lat,
                    "attempts": len(attempts) + 1,
                    "ok": True,
                }
            attempts.append(
                {"backend": backend, "error": "empty_response", "lat_ms": lat}
            )
        except Exception as e:
            lat = round((time.perf_counter() - t0) * 1000)
            r.hincrby(STATS_KEY, f"{backend}:fail", 1)
            attempts.append({"backend": backend, "error": str(e)[:60], "lat_ms": lat})

    r.hincrby(STATS_KEY, f"{task_type}:all_failed", 1)
    return {"text": "", "ok": False, "attempts": attempts}


def stats() -> dict:
    s = r.hgetall(STATS_KEY)
    return {k: int(v) for k, v in s.items()}


if __name__ == "__main__":
    for task, prompt in [
        ("fast", "1+1="),
        ("code", "def hello(): pass  # explain this"),
    ]:
        result = ask(prompt, task)
        if result["ok"]:
            print(
                f"  [{task}] → {result['backend']} ({result['latency_ms']}ms): {result['text'][:60]}"
            )
        else:
            print(f"  [{task}] FAILED: {result['attempts']}")
