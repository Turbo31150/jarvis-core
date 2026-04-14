#!/usr/bin/env python3
"""JARVIS A/B Router — Route requests across model variants with traffic splitting and metrics"""

import redis
import json
import time
import hashlib
import random

r = redis.Redis(decode_responses=True)

EXP_PREFIX = "jarvis:ab:exp:"
INDEX_KEY = "jarvis:ab:experiments"
RESULT_PREFIX = "jarvis:ab:result:"
STATS_KEY = "jarvis:ab:stats"


def create_experiment(name: str, variants: list, description: str = "") -> str:
    """
    variants: [{"name": "control", "model": "m32_mistral", "weight": 0.7},
               {"name": "test",    "model": "ol1_gemma3",  "weight": 0.3}]
    """
    eid = hashlib.md5(name.encode()).hexdigest()[:10]
    exp = {
        "id": eid,
        "name": name,
        "description": description,
        "variants": variants,
        "status": "active",
        "created_at": time.time(),
    }
    r.setex(f"{EXP_PREFIX}{eid}", 86400 * 30, json.dumps(exp))
    r.sadd(INDEX_KEY, eid)
    r.hincrby(STATS_KEY, "experiments_created", 1)
    return eid


def assign_variant(exp_name: str, request_id: str) -> dict | None:
    """Deterministically assign a variant based on request_id hash."""
    eid = hashlib.md5(exp_name.encode()).hexdigest()[:10]
    raw = r.get(f"{EXP_PREFIX}{eid}")
    if not raw:
        return None
    exp = json.loads(raw)
    if exp["status"] != "active":
        return None

    # Deterministic bucket 0-99 from request_id
    bucket = int(hashlib.md5(f"{eid}:{request_id}".encode()).hexdigest(), 16) % 100

    # Assign based on cumulative weights
    cumulative = 0.0
    total_weight = sum(v["weight"] for v in exp["variants"])
    for variant in exp["variants"]:
        cumulative += (variant["weight"] / total_weight) * 100
        if bucket < cumulative:
            r.hincrby(f"{RESULT_PREFIX}{eid}:{variant['name']}", "assigned", 1)
            return {
                "experiment": exp_name,
                "variant": variant["name"],
                "model": variant["model"],
                "bucket": bucket,
            }
    return None


def record_outcome(
    exp_name: str,
    variant_name: str,
    latency_ms: float,
    success: bool,
    score: float = None,
):
    eid = hashlib.md5(exp_name.encode()).hexdigest()[:10]
    key = f"{RESULT_PREFIX}{eid}:{variant_name}"
    r.hincrby(key, "requests", 1)
    if success:
        r.hincrby(key, "successes", 1)
    # EWA latency
    ewa_key = f"{key}:lat_ewa"
    prev = float(r.get(ewa_key) or latency_ms)
    r.set(ewa_key, round(0.1 * latency_ms + 0.9 * prev, 1))
    if score is not None:
        score_key = f"{key}:score_ewa"
        prev_s = float(r.get(score_key) or score)
        r.set(score_key, round(0.1 * score + 0.9 * prev_s, 3))
    r.expire(key, 86400 * 30)


def get_results(exp_name: str) -> dict:
    eid = hashlib.md5(exp_name.encode()).hexdigest()[:10]
    raw = r.get(f"{EXP_PREFIX}{eid}")
    if not raw:
        return {}
    exp = json.loads(raw)
    results = {}
    for v in exp["variants"]:
        key = f"{RESULT_PREFIX}{eid}:{v['name']}"
        data = r.hgetall(key)
        reqs = int(data.get("requests", 0))
        succ = int(data.get("successes", 0))
        lat = float(r.get(f"{key}:lat_ewa") or 0)
        score = float(r.get(f"{key}:score_ewa") or 0)
        results[v["name"]] = {
            "model": v["model"],
            "weight": v["weight"],
            "requests": reqs,
            "success_rate": round(succ / max(reqs, 1) * 100, 1),
            "lat_ewa_ms": lat,
            "score_ewa": score,
        }
    return {"experiment": exp_name, "variants": results}


def stop_experiment(exp_name: str):
    eid = hashlib.md5(exp_name.encode()).hexdigest()[:10]
    raw = r.get(f"{EXP_PREFIX}{eid}")
    if raw:
        exp = json.loads(raw)
        exp["status"] = "stopped"
        r.setex(f"{EXP_PREFIX}{eid}", 86400 * 7, json.dumps(exp))


def stats() -> dict:
    s = r.hgetall(STATS_KEY)
    return {k: int(v) for k, v in s.items()}


if __name__ == "__main__":
    exp_id = create_experiment(
        "mistral_vs_gemma",
        variants=[
            {"name": "control", "model": "m32_mistral", "weight": 0.6},
            {"name": "test", "model": "ol1_gemma3", "weight": 0.4},
        ],
        description="Compare mistral vs gemma3 on latency and quality",
    )

    profiles = {
        "control": {"lat": 130, "succ_rate": 0.98, "score": 0.83},
        "test": {"lat": 380, "succ_rate": 0.94, "score": 0.76},
    }

    for i in range(100):
        rid = f"req_{i:04d}"
        assignment = assign_variant("mistral_vs_gemma", rid)
        if assignment:
            p = profiles[assignment["variant"]]
            lat = max(50, random.gauss(p["lat"], p["lat"] * 0.2))
            ok = random.random() < p["succ_rate"]
            score = random.gauss(p["score"], 0.1) if ok else 0.2
            record_outcome(
                "mistral_vs_gemma",
                assignment["variant"],
                lat,
                ok,
                max(0, min(1, score)),
            )

    print("A/B Experiment Results: mistral_vs_gemma")
    res = get_results("mistral_vs_gemma")
    for vname, vdata in res["variants"].items():
        print(
            f"  {vname:8s} ({vdata['model']:15s}) w={vdata['weight']}  "
            f"n={vdata['requests']:3d}  succ={vdata['success_rate']:5.1f}%  "
            f"lat={vdata['lat_ewa_ms']:6.0f}ms  score={vdata['score_ewa']:.3f}"
        )

    print(f"\nStats: {stats()}")
