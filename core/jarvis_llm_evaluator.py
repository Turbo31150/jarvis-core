#!/usr/bin/env python3
"""JARVIS LLM Evaluator — Score and compare LLM responses across backends"""

import redis
import json
import time
import re

r = redis.Redis(decode_responses=True)

EVAL_KEY = "jarvis:llm_eval:results"
LEADERBOARD_KEY = "jarvis:llm_eval:leaderboard"


def score_response(response: str, criteria: dict = None) -> dict:
    """Score a response on multiple axes (0-10 each)."""
    if not response:
        return {"total": 0, "breakdown": {}}

    c = criteria or {}
    scores = {}

    # Length score — penalize too short or absurdly long
    length = len(response)
    if length < 20:
        scores["length"] = 2
    elif length < 100:
        scores["length"] = 6
    elif length < 2000:
        scores["length"] = 10
    else:
        scores["length"] = 7

    # Structure — lists, headers, code blocks
    has_list = bool(re.search(r"^\s*[-*•]\s", response, re.MULTILINE))
    has_code = "```" in response or "    " in response
    has_headers = bool(re.search(r"^#{1,3}\s", response, re.MULTILINE))
    scores["structure"] = sum([has_list * 3, has_code * 4, has_headers * 3])

    # No preamble — penalize "Sure!", "Certainly!", "Of course!"
    preamble = bool(
        re.match(r"(Sure|Certainly|Of course|Absolutely|Great)[!,.]", response)
    )
    scores["directness"] = 3 if preamble else 10

    # Language coherence — basic check
    words = response.split()
    unique_ratio = len(set(words)) / max(len(words), 1)
    scores["coherence"] = min(10, round(unique_ratio * 14))

    # Task-specific scoring
    if c.get("must_contain"):
        found = sum(1 for kw in c["must_contain"] if kw.lower() in response.lower())
        scores["relevance"] = round(found / len(c["must_contain"]) * 10)
    else:
        scores["relevance"] = 8  # default neutral

    total = round(sum(scores.values()) / len(scores))
    return {"total": total, "breakdown": scores, "length": length}


def evaluate_backend(
    backend: str, prompt: str, response: str, latency_ms: float
) -> dict:
    """Record an evaluation result for a backend."""
    score = score_response(response)
    entry = {
        "backend": backend,
        "prompt_hash": hash(prompt) % 100000,
        "score": score["total"],
        "latency_ms": latency_ms,
        "efficiency": round(score["total"] / max(latency_ms / 1000, 0.1), 2),
        "ts": time.time(),
    }
    r.lpush(EVAL_KEY, json.dumps(entry))
    r.ltrim(EVAL_KEY, 0, 499)
    # Update leaderboard (score = cumulative efficiency)
    r.zincrby(LEADERBOARD_KEY, entry["efficiency"], backend)
    return entry


def leaderboard() -> list:
    raw = r.zrevrange(LEADERBOARD_KEY, 0, -1, withscores=True)
    return [{"backend": b, "cumulative_efficiency": round(s, 1)} for b, s in raw]


def recent_evals(limit: int = 20) -> list:
    raw = r.lrange(EVAL_KEY, 0, limit - 1)
    return [json.loads(x) for x in raw]


def stats() -> dict:
    evals = recent_evals(200)
    if not evals:
        return {"total": 0}
    by_backend = {}
    for e in evals:
        b = e["backend"]
        if b not in by_backend:
            by_backend[b] = {"scores": [], "latencies": []}
        by_backend[b]["scores"].append(e["score"])
        by_backend[b]["latencies"].append(e["latency_ms"])
    result = {}
    for b, d in by_backend.items():
        result[b] = {
            "count": len(d["scores"]),
            "avg_score": round(sum(d["scores"]) / len(d["scores"]), 1),
            "avg_latency_ms": round(sum(d["latencies"]) / len(d["latencies"])),
        }
    return {"total": len(evals), "by_backend": result, "leaderboard": leaderboard()}


if __name__ == "__main__":
    samples = [
        (
            "m2",
            "Explain neural networks",
            "Neural networks are models inspired by the brain.\n- Layer 1: input\n- Layer 2: hidden\n- Layer 3: output\n```python\nmodel = Sequential()\n```",
            850,
        ),
        (
            "m32",
            "Explain neural networks",
            "Sure! Neural networks are great! They consist of layers...",
            420,
        ),
        (
            "ol1",
            "Explain neural networks",
            "NNs stack layers of weighted nodes. Each layer transforms input via activation functions.",
            200,
        ),
        ("m2", "What is 2+2?", "4", 300),
        ("ol1", "What is 2+2?", "2+2=4", 150),
    ]
    for backend, prompt, resp, lat in samples:
        e = evaluate_backend(backend, prompt, resp, lat)
        print(f"  {backend}: score={e['score']}/10 lat={lat}ms eff={e['efficiency']}")
    print(f"\nLeaderboard: {leaderboard()}")
