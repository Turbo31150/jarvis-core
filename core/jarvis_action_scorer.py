#!/usr/bin/env python3
"""JARVIS Action Scorer — Note chaque action 0-10, stocke dans etoile.db + Redis"""
import sqlite3, redis, json, time
from datetime import datetime

DB = "/home/turbo/IA/Core/jarvis/core/memory/etoile.db"
r = redis.Redis(decode_responses=True)

CRITERIA = {
    "success":   3,   # action réussie
    "fast":      2,   # < 2s
    "cached":    1,   # depuis cache LLM
    "novel":     2,   # nouvelle capacité ajoutée
    "error":    -3,   # erreur
    "timeout":  -2,   # timeout
    "fallback": -1,   # dégradé
}

def score_action(action: str, outcome: dict) -> int:
    score = 5  # base
    if outcome.get("success"): score += CRITERIA["success"]
    if outcome.get("elapsed_ms", 9999) < 2000: score += CRITERIA["fast"]
    if outcome.get("from_cache"): score += CRITERIA["cached"]
    if outcome.get("novel"): score += CRITERIA["novel"]
    if outcome.get("error"): score += CRITERIA["error"]
    if outcome.get("timeout"): score += CRITERIA["timeout"]
    if outcome.get("fallback"): score += CRITERIA["fallback"]
    return max(0, min(10, score))

def log_action(action: str, agent: str, outcome: dict):
    s = score_action(action, outcome)
    ts = datetime.now().isoformat()
    # Redis: score glissant
    r.lpush("jarvis:action_scores", json.dumps({"action": action, "agent": agent, "score": s, "ts": ts}))
    r.ltrim("jarvis:action_scores", 0, 999)
    # Running average
    scores = [json.loads(x)["score"] for x in r.lrange("jarvis:action_scores", 0, 99)]
    avg = sum(scores) / len(scores) if scores else 0
    r.setex("jarvis:avg_score", 3600, f"{avg:.1f}")
    return s

def get_stats():
    scores = [json.loads(x)["score"] for x in r.lrange("jarvis:action_scores", 0, 99)]
    if not scores: return {"avg": 0, "count": 0}
    return {"avg": round(sum(scores)/len(scores), 1), "count": len(scores),
            "min": min(scores), "max": max(scores)}

if __name__ == "__main__":
    # Test
    s = log_action("test_benchmark", "claude", {"success": True, "elapsed_ms": 500})
    print(f"Score action test: {s}/10")
    print(f"Stats: {get_stats()}")
