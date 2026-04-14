#!/usr/bin/env python3
"""JARVIS LLM Cost Optimizer — Choose cheapest model meeting quality requirements"""
import redis, json
from datetime import datetime

r = redis.Redis(decode_responses=True)

# quality_score (0-10), cost ($/1k tokens), avg_latency_ms
MODELS = {
    "gemma3:4b":              {"quality": 4, "cost": 0.0,   "latency": 800,  "backend": "ol1"},
    "qwen3:1.7b":             {"quality": 3, "cost": 0.0,   "latency": 400,  "backend": "ol1", "blacklisted": True},
    "qwen/qwen3.5-9b":        {"quality": 6, "cost": 0.0,   "latency": 2000, "backend": "m2"},
    "qwen/qwen3.5-35b-a3b":   {"quality": 8, "cost": 0.0,   "latency": 8000, "backend": "m2"},
    "deepseek/deepseek-r1-0528-qwen3-8b": {"quality": 7, "cost": 0.0, "latency": 5000, "backend": "m2"},
    "gemini-2.5-flash":       {"quality": 9, "cost": 0.0,   "latency": 3000, "backend": "gemini"},
    "claude-sonnet-4.6":      {"quality": 10,"cost": 0.015, "latency": 2000, "backend": "claude"},
}

def recommend(min_quality: int = 5, max_latency_ms: int = 10000, free_only: bool = True) -> list:
    candidates = [
        {"model": m, **info}
        for m, info in MODELS.items()
        if not info.get("blacklisted")
        and info["quality"] >= min_quality
        and info["latency"] <= max_latency_ms
        and (not free_only or info["cost"] == 0.0)
    ]
    return sorted(candidates, key=lambda x: (-x["quality"], x["latency"]))

def cheapest_for_quality(task: str = "default") -> dict:
    TASK_REQUIREMENTS = {
        "fast":      {"min_quality": 3, "max_latency_ms": 1500},
        "classify":  {"min_quality": 4, "max_latency_ms": 2000},
        "summary":   {"min_quality": 5, "max_latency_ms": 5000},
        "code":      {"min_quality": 7, "max_latency_ms": 15000},
        "trading":   {"min_quality": 7, "max_latency_ms": 15000},
        "default":   {"min_quality": 6, "max_latency_ms": 10000},
    }
    req = TASK_REQUIREMENTS.get(task, TASK_REQUIREMENTS["default"])
    candidates = recommend(**req)
    return candidates[0] if candidates else {}

if __name__ == "__main__":
    print("Model recommendations:")
    for task in ["fast", "classify", "code", "trading"]:
        best = cheapest_for_quality(task)
        if best:
            print(f"  {task}: {best['model']} (quality={best['quality']}, {best['latency']}ms, free={best['cost']==0})")
