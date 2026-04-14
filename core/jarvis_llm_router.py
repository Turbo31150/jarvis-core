#!/usr/bin/env python3
"""JARVIS LLM Router — Route tasks to specialized GPU/model based on task type"""

import redis
import requests
import time

r = redis.Redis(decode_responses=True)

# Model specializations: task_type → (host, model, timeout)
ROUTING_TABLE = {
    "code": ("http://192.168.1.26:1234", "qwen/qwen3.5-35b-a3b", 45),
    "reasoning": ("http://192.168.1.26:1234", "deepseek/deepseek-r1-0528-qwen3-8b", 60),
    "trading": ("http://192.168.1.26:1234", "deepseek/deepseek-r1-0528-qwen3-8b", 60),
    "summary": ("http://127.0.0.1:11434", "gemma3:4b", 30),
    "classify": ("http://127.0.0.1:11434", "gemma3:4b", 15),
    "fast": ("http://127.0.0.1:11434", "gemma3:4b", 10),
    "default": ("http://192.168.1.26:1234", "qwen/qwen3.5-35b-a3b", 45),
    # M32 (192.168.1.113) — nouveau nœud avec deepseek-r1 + mistral
    "instruct": ("http://192.168.1.113:1234", "mistral-7b-instruct-v0.3", 30),
    "reasoning_m32": (
        "http://192.168.1.113:1234",
        "deepseek/deepseek-r1-0528-qwen3-8b",
        60,
    ),
    "embedding": (
        "http://192.168.1.113:1234",
        "text-embedding-nomic-embed-text-v1.5",
        15,
    ),
}


def detect_type(prompt: str) -> str:
    p = prompt.lower()
    if any(
        w in p
        for w in ["def ", "class ", "import ", "function", "bug", "fix", "refactor"]
    ):
        return "code"
    if any(
        w in p for w in ["trade", "btc", "eth", "signal", "long", "short", "market"]
    ):
        return "trading"
    if any(w in p for w in ["résume", "resume", "summarize", "résumé"]):
        return "summary"
    if any(w in p for w in ["classe", "catégorise", "classify", "quel type"]):
        return "classify"
    if len(prompt) < 80:
        return "fast"
    return "default"


def ask(prompt: str, task_type: str = None, stream: bool = False) -> str:
    t_type = task_type or detect_type(prompt)
    host, model, timeout = ROUTING_TABLE.get(t_type, ROUTING_TABLE["default"])

    # Backend name for circuit breaker + rate limiter
    backend = "ol1" if "11434" in host else ("m1" if "1.85" in host else "m2")

    try:
        from jarvis_circuit_breaker import is_allowed, record_success, record_failure
        from jarvis_rate_limiter import check_and_consume

        if not is_allowed(backend):
            return f"[circuit_open:{backend}] try later"
        if not check_and_consume(backend):
            return f"[rate_limited:{backend}]"
    except ImportError:
        pass

    r.incr(f"jarvis:llm_router:{t_type}:count")
    t0 = time.perf_counter()

    try:
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
            result = resp.json().get("response", "")
        else:
            resp = requests.post(
                f"{host}/v1/chat/completions",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 800,
                    "temperature": 0.1,
                },
                timeout=timeout,
            )
            msg = resp.json()["choices"][0]["message"]
            result = msg.get("content") or msg.get("reasoning_content", "")

        lat = round((time.perf_counter() - t0) * 1000)
        r.setex(f"jarvis:llm_router:{t_type}:last_ms", 300, lat)
        try:
            from jarvis_circuit_breaker import record_success

            record_success(backend)
        except ImportError:
            pass
        return result
    except Exception as e:
        try:
            from jarvis_circuit_breaker import record_failure

            record_failure(backend)
        except ImportError:
            pass
        r.incr(f"jarvis:llm_router:{t_type}:errors")
        return f"[router_error:{t_type}] {e}"


def stats() -> dict:
    res = {}
    for t in ROUTING_TABLE:
        cnt = r.get(f"jarvis:llm_router:{t}:count") or "0"
        err = r.get(f"jarvis:llm_router:{t}:errors") or "0"
        lat = r.get(f"jarvis:llm_router:{t}:last_ms") or "?"
        if int(cnt) > 0:
            res[t] = {"requests": int(cnt), "errors": int(err), "last_ms": lat}
    return res


if __name__ == "__main__":
    print("Testing router...")
    r2 = ask("1+1=", "fast")
    print(f"fast: {r2[:30]}")
    print("Stats:", stats())
