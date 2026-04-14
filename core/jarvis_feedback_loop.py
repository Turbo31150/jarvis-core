#!/usr/bin/env python3
"""JARVIS Feedback Loop — Automatic quality scoring and routing adjustment"""

import redis
import json
import time
from datetime import datetime

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:feedback"


def record_response(
    backend: str,
    task_type: str,
    prompt: str,
    response: str,
    latency_ms: int,
    user_rating: float = None,
) -> str:
    """Record an LLM response with automatic quality signals"""
    fid = f"fb:{int(time.time())}"

    # Auto-quality signals
    quality_signals = {
        "length_ok": 10 <= len(response) <= 4000,
        "not_empty": len(response.strip()) > 0,
        "no_error_prefix": not response.startswith("[router_error") and not response.startswith("[circuit"),
        "latency_ok": latency_ms < 10000,
    }
    auto_score = sum(1 for v in quality_signals.values() if v) / len(quality_signals)
    final_score = user_rating if user_rating is not None else auto_score

    entry = {
        "id": fid,
        "backend": backend,
        "task_type": task_type,
        "prompt_preview": prompt[:80],
        "response_len": len(response),
        "latency_ms": latency_ms,
        "auto_score": round(auto_score, 2),
        "final_score": round(final_score, 2),
        "quality_signals": quality_signals,
        "ts": datetime.now().isoformat()[:19],
    }
    r.lpush(f"{PREFIX}:{backend}:{task_type}", json.dumps(entry))
    r.ltrim(f"{PREFIX}:{backend}:{task_type}", 0, 99)
    r.expire(f"{PREFIX}:{backend}:{task_type}", 86400)

    # Running average
    r.hincrbyfloat(f"{PREFIX}:avg:{backend}", "score_sum", final_score)
    r.hincrby(f"{PREFIX}:avg:{backend}", "count", 1)
    return fid


def get_backend_score(backend: str) -> float:
    data = r.hgetall(f"{PREFIX}:avg:{backend}")
    if not data or not data.get("count"):
        return 0.5  # neutral default
    return round(float(data["score_sum"]) / int(data["count"]), 3)


def get_best_backend(task_type: str = "default") -> str:
    """Return backend with highest feedback score"""
    backends = ["m2", "ol1", "m1"]
    scores = {b: get_backend_score(b) for b in backends}
    return max(scores, key=scores.get)


def auto_adjust_routing() -> dict:
    """Suggest routing adjustments based on feedback scores"""
    adjustments = []
    for backend in ["m2", "ol1", "m1"]:
        score = get_backend_score(backend)
        if score < 0.5:
            adjustments.append({
                "backend": backend,
                "score": score,
                "action": "reduce_traffic",
                "reason": "Low quality score",
            })
        elif score > 0.9:
            adjustments.append({
                "backend": backend,
                "score": score,
                "action": "increase_traffic",
                "reason": "High quality score",
            })
    return {"adjustments": adjustments, "scores": {b: get_backend_score(b) for b in ["m2", "ol1", "m1"]}}


def stats() -> dict:
    result = {}
    for backend in ["m2", "ol1", "m1"]:
        data = r.hgetall(f"{PREFIX}:avg:{backend}")
        count = int(data.get("count", 0))
        score = get_backend_score(backend)
        result[backend] = {"responses": count, "avg_score": score}
    return result


if __name__ == "__main__":
    # Simulate feedback recording
    record_response("ol1", "fast", "What is 2+2?", "The answer is 4.", 850)
    record_response("m2", "code", "Write a hello world", "print('Hello, World!')", 1200)
    record_response("ol1", "fast", "Capital of France?", "Paris.", 700)
    record_response("m2", "fast", "Short question", "", 50)  # empty = bad

    print(f"Stats: {stats()}")
    adj = auto_adjust_routing()
    print(f"Scores: {adj['scores']}")
    if adj["adjustments"]:
        for a in adj["adjustments"]:
            print(f"  → {a['backend']}: {a['action']} (score={a['score']})")
    else:
        print("No routing adjustments needed")
    print(f"Best backend: {get_best_backend()}")
