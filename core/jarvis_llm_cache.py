#!/usr/bin/env python3
"""JARVIS LLM Response Cache — Redis-based, TTL 1h par défaut"""
import redis, hashlib, json, time

r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)

def cache_key(model: str, prompt: str) -> str:
    h = hashlib.sha256(f"{model}:{prompt}".encode()).hexdigest()[:16]
    return f"jarvis:llm_cache:{h}"

def get(model: str, prompt: str):
    key = cache_key(model, prompt)
    val = r.get(key)
    if val:
        r.incr("jarvis:cache_hits")
        return json.loads(val)
    r.incr("jarvis:cache_misses")
    return None

def set(model: str, prompt: str, response: str, ttl: int = 3600):
    key = cache_key(model, prompt)
    r.setex(key, ttl, json.dumps({"response": response, "model": model, "ts": time.time()}))

def stats():
    hits = int(r.get("jarvis:cache_hits") or 0)
    misses = int(r.get("jarvis:cache_misses") or 0)
    total = hits + misses
    rate = (hits/total*100) if total > 0 else 0
    keys = len(r.keys("jarvis:llm_cache:*"))
    return {"hit_rate": f"{rate:.1f}%", "hits": hits, "misses": misses, "cached_entries": keys}

if __name__ == "__main__":
    # Test
    set("qwen3.5-9b", "test prompt", "test response", ttl=60)
    result = get("qwen3.5-9b", "test prompt")
    print("Cache test:", result)
    print("Stats:", stats())
