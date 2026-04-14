#!/usr/bin/env python3
"""JARVIS Model Router v2 — Smart routing with latency-aware load balancing and A/B splits"""

import redis
import time
import random
import requests

r = redis.Redis(decode_responses=True)

BACKENDS = {
    "m1": {
        "url": "http://192.168.1.85:1234",
        "model": "qwen3.5-27b-claude-4.6-opus-distilled",
        "weight": 1,
    },
    "m2": {
        "url": "http://192.168.1.26:1234",
        "model": "qwen/qwen3.5-35b-a3b",
        "weight": 2,
    },
    "m32": {
        "url": "http://192.168.1.113:1234",
        "model": "mistral-7b-instruct-v0.3",
        "weight": 2,
    },
    "ol1": {"url": "http://127.0.0.1:11434", "model": "gemma3:4b", "weight": 3},
}

TASK_ROUTES = {
    "fast": ["ol1", "m32"],
    "code": ["m2", "m1", "m32"],
    "reasoning": ["m2", "m32", "ol1"],
    "instruct": ["m32", "m2", "ol1"],
    "embed": ["m32"],
    "default": ["m2", "m32", "ol1"],
}

EWA_ALPHA = 0.3  # exponential weighted average for latency


def _get_latency(backend: str) -> float:
    v = r.get(f"jarvis:router_v2:{backend}:ewa_lat")
    return float(v) if v else 500.0


def _update_latency(backend: str, lat_ms: float):
    prev = _get_latency(backend)
    ewa = EWA_ALPHA * lat_ms + (1 - EWA_ALPHA) * prev
    r.setex(f"jarvis:router_v2:{backend}:ewa_lat", 600, round(ewa, 1))


def _is_healthy(backend: str) -> bool:
    state = r.hget(f"jarvis:cb:{backend}", "state")
    return state != "open"


def select_backend(task_type: str = "default", strategy: str = "latency") -> str:
    """Select optimal backend. Strategies: latency, round_robin, weighted_random."""
    candidates = TASK_ROUTES.get(task_type, TASK_ROUTES["default"])
    healthy = [b for b in candidates if _is_healthy(b)]
    if not healthy:
        healthy = [b for b in BACKENDS if _is_healthy(b)] or list(BACKENDS.keys())

    if strategy == "latency":
        return min(healthy, key=_get_latency)
    elif strategy == "weighted_random":
        weights = [BACKENDS[b]["weight"] for b in healthy]
        return random.choices(healthy, weights=weights)[0]
    else:  # round_robin
        idx = int(r.incr(f"jarvis:router_v2:rr:{task_type}")) % len(healthy)
        return healthy[idx]


def route(prompt: str, task_type: str = "default", max_tokens: int = 500) -> dict:
    backend_name = select_backend(task_type)
    cfg = BACKENDS[backend_name]
    t0 = time.perf_counter()

    try:
        if "11434" in cfg["url"]:
            resp = requests.post(
                f"{cfg['url']}/api/generate",
                json={
                    "model": cfg["model"],
                    "prompt": prompt,
                    "stream": False,
                    "options": {"num_predict": max_tokens},
                },
                timeout=20,
            )
            text = resp.json().get("response", "")
        else:
            resp = requests.post(
                f"{cfg['url']}/v1/chat/completions",
                json={
                    "model": cfg["model"],
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": max_tokens,
                    "temperature": 0.1,
                },
                timeout=20,
            )
            choices = resp.json().get("choices", [])
            text = choices[0]["message"].get("content", "") if choices else ""

        lat = round((time.perf_counter() - t0) * 1000)
        _update_latency(backend_name, lat)
        r.incr(f"jarvis:router_v2:{backend_name}:ok")
        return {"backend": backend_name, "text": text, "latency_ms": lat, "ok": True}

    except Exception as e:
        lat = round((time.perf_counter() - t0) * 1000)
        _update_latency(backend_name, lat + 2000)  # penalize
        r.incr(f"jarvis:router_v2:{backend_name}:err")
        return {
            "backend": backend_name,
            "error": str(e)[:100],
            "latency_ms": lat,
            "ok": False,
        }


def stats() -> dict:
    result = {}
    for b in BACKENDS:
        ok = int(r.get(f"jarvis:router_v2:{b}:ok") or 0)
        err = int(r.get(f"jarvis:router_v2:{b}:err") or 0)
        if ok + err > 0:
            result[b] = {"ok": ok, "err": err, "ewa_lat_ms": _get_latency(b)}
    return result


if __name__ == "__main__":
    for task in ["fast", "code", "reasoning"]:
        backend = select_backend(task, "latency")
        lat = _get_latency(backend)
        print(f"  {task:12s} → {backend} (ewa={lat:.0f}ms)")
    print(f"Stats: {stats()}")
