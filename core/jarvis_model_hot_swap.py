#!/usr/bin/env python3
"""JARVIS Model Hot Swap — Switch active LLM models without service restart"""
import requests, redis, json
from datetime import datetime

r = redis.Redis(decode_responses=True)

BACKENDS = {
    "m2": "http://192.168.1.26:1234",
    "ol1": "http://127.0.0.1:11434",
}

def get_loaded_models(backend: str) -> list:
    host = BACKENDS.get(backend, "")
    try:
        if "11434" in host:
            resp = requests.get(f"{host}/api/ps", timeout=5)
            return [m["name"] for m in resp.json().get("models", [])]
        else:
            # LM Studio — currently loaded models
            resp = requests.get(f"{host}/v1/models", timeout=5)
            return [m["id"] for m in resp.json().get("data", [])]
    except:
        return []

def swap_active_route(task_type: str, new_model: str, backend: str):
    """Update routing table to use a different model for a task type"""
    key = f"jarvis:routing_override:{task_type}"
    r.setex(key, 3600, json.dumps({"model": new_model, "backend": backend,
                                    "ts": datetime.now().isoformat()[:19]}))
    r.publish("jarvis:events", json.dumps({
        "type": "model_hot_swap",
        "data": {"task_type": task_type, "model": new_model, "backend": backend},
        "severity": "info", "ts": datetime.now().isoformat()[:19]
    }))
    return True

def get_active_overrides() -> dict:
    overrides = {}
    for key in r.scan_iter("jarvis:routing_override:*"):
        task_type = key.replace("jarvis:routing_override:", "")
        raw = r.get(key)
        if raw:
            overrides[task_type] = json.loads(raw)
    return overrides

def revert(task_type: str):
    r.delete(f"jarvis:routing_override:{task_type}")

if __name__ == "__main__":
    print("Loaded models:")
    for backend in ["m2", "ol1"]:
        models = get_loaded_models(backend)
        print(f"  {backend}: {models[:3]}")
    
    # Test hot swap
    swap_active_route("code", "deepseek/deepseek-r1-0528-qwen3-8b", "m2")
    print("Overrides:", get_active_overrides())
    revert("code")
    print("After revert:", get_active_overrides())
