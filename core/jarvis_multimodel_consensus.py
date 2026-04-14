#!/usr/bin/env python3
"""JARVIS Multi-Model Consensus — Query multiple LLMs and aggregate responses by voting/scoring"""

import redis
import json
import time
import hashlib
import requests
import threading

r = redis.Redis(decode_responses=True)

STATS_KEY = "jarvis:consensus:stats"
LOG_KEY = "jarvis:consensus:log"

BACKENDS = {
    "m32_mistral": {
        "url": "http://192.168.1.113:1234/v1/chat/completions",
        "model": "mistral-7b-instruct-v0.3",
        "weight": 1.0,
    },
    "ol1_gemma3": {
        "url": "http://127.0.0.1:11434/api/chat",
        "model": "gemma3:4b",
        "weight": 0.7,
        "ollama": True,
    },
}


def _call_backend(name: str, cfg: dict, prompt: str, results: dict, timeout: int = 20):
    t0 = time.perf_counter()
    try:
        if cfg.get("ollama"):
            resp = requests.post(
                cfg["url"],
                json={
                    "model": cfg["model"],
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                },
                timeout=timeout,
            )
            text = resp.json()["message"]["content"].strip()
        else:
            resp = requests.post(
                cfg["url"],
                json={
                    "model": cfg["model"],
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 200,
                    "temperature": 0.1,
                },
                timeout=timeout,
            )
            text = resp.json()["choices"][0]["message"]["content"].strip()
        lat = round((time.perf_counter() - t0) * 1000)
        results[name] = {
            "text": text,
            "latency_ms": lat,
            "ok": True,
            "weight": cfg["weight"],
        }
    except Exception as e:
        lat = round((time.perf_counter() - t0) * 1000)
        results[name] = {
            "ok": False,
            "error": str(e)[:60],
            "latency_ms": lat,
            "weight": 0,
        }


def query_all(prompt: str, timeout: int = 25) -> dict:
    """Query all backends in parallel."""
    results = {}
    threads = [
        threading.Thread(target=_call_backend, args=(n, cfg, prompt, results, timeout))
        for n, cfg in BACKENDS.items()
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout + 2)
    return results


def majority_vote(responses: dict) -> dict:
    """Simple majority: return most common non-empty response."""
    valid = {k: v for k, v in responses.items() if v.get("ok") and v.get("text")}
    if not valid:
        return {"consensus": None, "method": "no_valid_responses"}

    # Group by normalized text (strip, lower, truncate to 50 chars)
    groups: dict[str, list] = {}
    for name, resp in valid.items():
        key = resp["text"].lower().strip()[:60]
        groups.setdefault(key, []).append((name, resp))

    # Weighted vote
    best_key = max(groups, key=lambda k: sum(r_[1]["weight"] for r_ in groups[k]))
    winners = groups[best_key]
    # Return highest-weight response text
    best = max(winners, key=lambda x: x[1]["weight"])
    return {
        "consensus": best[1]["text"],
        "source": best[0],
        "votes": len(winners),
        "total_valid": len(valid),
        "method": "weighted_majority",
    }


def synthesize(responses: dict, question: str = "") -> dict:
    """Return consensus result with metadata."""
    valid = {k: v for k, v in responses.items() if v.get("ok")}
    if not valid:
        return {"consensus": "[ALL BACKENDS FAILED]", "confidence": 0.0}

    consensus = majority_vote(responses)
    confidence = consensus["votes"] / max(len(valid), 1)

    result = {
        "consensus": consensus["consensus"],
        "confidence": round(confidence, 2),
        "source": consensus.get("source"),
        "method": consensus["method"],
        "responses": {
            k: {
                "text": v.get("text", "")[:100],
                "latency_ms": v.get("latency_ms", 0),
                "ok": v.get("ok", False),
            }
            for k, v in responses.items()
        },
        "ts": time.time(),
    }
    cid = hashlib.md5(f"{question}:{time.time()}".encode()).hexdigest()[:8]
    r.lpush(LOG_KEY, json.dumps({**result, "question": question[:100], "id": cid}))
    r.ltrim(LOG_KEY, 0, 99)
    r.hincrby(STATS_KEY, "consensus_queries", 1)
    return result


def ask(prompt: str) -> dict:
    """High-level: query all, synthesize, return."""
    responses = query_all(prompt)
    return synthesize(responses, prompt)


def stats() -> dict:
    s = r.hgetall(STATS_KEY)
    return {k: int(v) for k, v in s.items()}


if __name__ == "__main__":
    questions = [
        "What is 2 + 2? Answer with just the number.",
        "Name the capital of France. Answer in one word.",
        "Is Python a compiled language? Answer yes or no.",
    ]

    print("Multi-Model Consensus:")
    for q in questions:
        result = ask(q)
        print(f"\n  Q: {q}")
        print(
            f"  Consensus: {result['consensus']!r}  "
            f"(confidence={result['confidence']:.0%}, source={result['source']})"
        )
        for backend, resp in result["responses"].items():
            icon = "✅" if resp["ok"] else "❌"
            txt = repr(resp["text"][:40]) if resp["ok"] else resp.get("error", "")[:40]
            print(f"    {icon} {backend:15s} {resp['latency_ms']:5d}ms  {txt}")

    print(f"\nStats: {stats()}")
