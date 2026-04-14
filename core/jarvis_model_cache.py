#!/usr/bin/env python3
"""JARVIS Model Cache — Track loaded models per node, VRAM usage, and eviction policy"""

import redis
import json
import time

r = redis.Redis(decode_responses=True)

LOADED_PREFIX = "jarvis:model_cache:"
NODE_PREFIX = "jarvis:model_cache:node:"

# Model VRAM requirements (GB)
MODEL_VRAM = {
    "qwen3.5-35b-a3b": 22.0,
    "qwen3.5-27b-claude-4.6-opus-distilled": 18.0,
    "deepseek-r1-0528-qwen3-8b": 8.0,
    "mistral-7b-instruct-v0.3": 6.0,
    "gemma3:4b": 3.5,
    "gemma-4-26b-a4b": 16.0,
    "phi-3.1-mini-128k": 4.0,
    "text-embedding-nomic-embed-text-v1.5": 0.5,
    "gpt-oss-20b": 14.0,
}

NODE_VRAM = {"M1": 12.0, "M2": 24.0, "M32": 10.0, "OL1": 8.0}


def load_model(node: str, model: str, force: bool = False) -> dict:
    vram_needed = MODEL_VRAM.get(model, 8.0)
    vram_total = NODE_VRAM.get(node, 8.0)
    vram_used = get_vram_used(node)

    if vram_used + vram_needed > vram_total and not force:
        # Try eviction of least recently used
        evicted = evict_lru(node, vram_needed)
        vram_used = get_vram_used(node)
        if vram_used + vram_needed > vram_total:
            return {
                "ok": False,
                "error": f"insufficient VRAM: need {vram_needed}GB, have {vram_total - vram_used:.1f}GB free",
                "evicted": evicted,
            }

    entry = {
        "node": node,
        "model": model,
        "vram_gb": vram_needed,
        "loaded_at": time.time(),
        "last_used": time.time(),
        "requests": 0,
    }
    r.setex(f"{LOADED_PREFIX}{node}:{model}", 86400, json.dumps(entry))
    r.sadd(f"{NODE_PREFIX}{node}:loaded", model)
    return {"ok": True, "node": node, "model": model, "vram_gb": vram_needed}


def unload_model(node: str, model: str) -> bool:
    r.delete(f"{LOADED_PREFIX}{node}:{model}")
    r.srem(f"{NODE_PREFIX}{node}:loaded", model)
    return True


def get_vram_used(node: str) -> float:
    models = r.smembers(f"{NODE_PREFIX}{node}:loaded")
    total = 0.0
    for m in models:
        raw = r.get(f"{LOADED_PREFIX}{node}:{m}")
        if raw:
            total += json.loads(raw).get("vram_gb", 0)
    return round(total, 1)


def evict_lru(node: str, need_gb: float) -> list:
    models = r.smembers(f"{NODE_PREFIX}{node}:loaded")
    entries = []
    for m in models:
        raw = r.get(f"{LOADED_PREFIX}{node}:{m}")
        if raw:
            e = json.loads(raw)
            entries.append((e["last_used"], m, e["vram_gb"]))
    entries.sort()  # oldest first
    evicted = []
    freed = 0.0
    for _, model, vram in entries:
        if freed >= need_gb:
            break
        unload_model(node, model)
        freed += vram
        evicted.append(model)
    return evicted


def record_use(node: str, model: str):
    key = f"{LOADED_PREFIX}{node}:{model}"
    raw = r.get(key)
    if raw:
        e = json.loads(raw)
        e["last_used"] = time.time()
        e["requests"] = e.get("requests", 0) + 1
        r.setex(key, 86400, json.dumps(e))


def get_loaded(node: str) -> list:
    models = r.smembers(f"{NODE_PREFIX}{node}:loaded")
    result = []
    for m in models:
        raw = r.get(f"{LOADED_PREFIX}{node}:{m}")
        if raw:
            result.append(json.loads(raw))
    return sorted(result, key=lambda x: x["last_used"], reverse=True)


def stats() -> dict:
    result = {}
    for node, total_vram in NODE_VRAM.items():
        used = get_vram_used(node)
        loaded = get_loaded(node)
        result[node] = {
            "vram_used_gb": used,
            "vram_total_gb": total_vram,
            "vram_free_gb": round(total_vram - used, 1),
            "models_loaded": len(loaded),
            "utilization_pct": round(used / total_vram * 100),
        }
    return result


if __name__ == "__main__":
    load_model("M2", "qwen3.5-35b-a3b")
    load_model("M2", "deepseek-r1-0528-qwen3-8b")
    load_model("M32", "mistral-7b-instruct-v0.3")
    load_model("M32", "text-embedding-nomic-embed-text-v1.5")
    load_model("OL1", "gemma3:4b")

    s = stats()
    for node, info in s.items():
        if info["models_loaded"] > 0:
            print(
                f"  {node}: {info['vram_used_gb']}/{info['vram_total_gb']}GB "
                f"({info['utilization_pct']}%) — {info['models_loaded']} models"
            )
