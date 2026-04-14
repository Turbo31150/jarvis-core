#!/usr/bin/env python3
"""JARVIS Model Registry — Centralized registry of all available LLM models"""
import requests, redis, json, time
from datetime import datetime

r = redis.Redis(decode_responses=True)

ENDPOINTS = [
    ("m1", "http://192.168.1.85:1234"),
    ("m2", "http://192.168.1.26:1234"),
    ("ol1", "http://127.0.0.1:11434"),
]

BLACKLIST = {"qwen3:1.7b", "qwen3.5-9b-mlx"}  # thinking bug

def discover_models() -> dict:
    registry = {"ts": datetime.now().isoformat()[:19], "backends": {}}
    
    for backend, host in ENDPOINTS:
        try:
            if "11434" in host:
                resp = requests.get(f"{host}/api/tags", timeout=5)
                models = [m["name"] for m in resp.json().get("models", [])
                          if m["name"] not in BLACKLIST]
            else:
                resp = requests.get(f"{host}/v1/models", timeout=5)
                models = [m["id"] for m in resp.json().get("data", [])
                          if m["id"] not in BLACKLIST]
            registry["backends"][backend] = {"host": host, "models": models, "up": True}
        except:
            registry["backends"][backend] = {"host": host, "models": [], "up": False}
    
    all_models = []
    for b, info in registry["backends"].items():
        for m in info["models"]:
            all_models.append({"model": m, "backend": b, "host": info["host"]})
    registry["all_models"] = all_models
    registry["total"] = len(all_models)
    
    r.setex("jarvis:model_registry", 300, json.dumps(registry))
    return registry

def get_best_for(task: str) -> dict:
    """Return best model for a given task type"""
    PREFERENCES = {
        "code":     ["qwen3.5-35b", "deepseek-r1", "qwen3.5-9b"],
        "trading":  ["deepseek-r1", "qwen3.5-35b"],
        "fast":     ["gemma3:4b", "gemma3"],
        "default":  ["qwen3.5-35b", "gemma3:4b"],
    }
    raw = r.get("jarvis:model_registry")
    if not raw:
        discover_models()
        raw = r.get("jarvis:model_registry")
    
    reg = json.loads(raw)
    prefs = PREFERENCES.get(task, PREFERENCES["default"])
    
    for pref in prefs:
        for m in reg["all_models"]:
            if pref.lower() in m["model"].lower():
                return m
    # fallback: first available
    return reg["all_models"][0] if reg["all_models"] else {}

if __name__ == "__main__":
    reg = discover_models()
    print(f"Total models: {reg['total']}")
    for b, info in reg["backends"].items():
        status = "✅" if info["up"] else "❌"
        print(f"  {status} {b}: {len(info['models'])} models")
        for m in info["models"][:3]:
            print(f"    - {m}")
