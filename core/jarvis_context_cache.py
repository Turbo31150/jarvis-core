#!/usr/bin/env python3
"""JARVIS Context Cache — met en cache les contextes LLM pour éviter re-prompting"""
import redis, json, hashlib, time
from datetime import datetime

r = redis.Redis(decode_responses=True)
TTL = 3600  # 1h cache

def cache_key(prompt: str, model: str) -> str:
    return f"jarvis:ctx:{model}:{hashlib.md5(prompt.encode()).hexdigest()[:12]}"

def get(prompt: str, model: str = "default") -> str | None:
    v = r.get(cache_key(prompt, model))
    if v:
        r.incr("jarvis:ctx:hits")
        return json.loads(v)["response"]
    r.incr("jarvis:ctx:misses")
    return None

def set(prompt: str, response: str, model: str = "default"):
    k = cache_key(prompt, model)
    r.setex(k, TTL, json.dumps({"response": response, "ts": datetime.now().isoformat()[:19], "model": model}))

def stats() -> dict:
    hits = int(r.get("jarvis:ctx:hits") or 0)
    misses = int(r.get("jarvis:ctx:misses") or 0)
    total = hits + misses
    return {"hits": hits, "misses": misses, "ratio": round(hits/total*100, 1) if total else 0}

if __name__ == "__main__":
    set("test", "cached response", "gemma3")
    print("get:", get("test", "gemma3"))
    print("stats:", stats())
