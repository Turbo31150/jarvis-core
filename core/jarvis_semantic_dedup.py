#!/usr/bin/env python3
"""JARVIS Semantic Dedup — Detect near-duplicate LLM requests to avoid redundant calls"""
import redis, json, hashlib
from datetime import datetime

r = redis.Redis(decode_responses=True)

def _ngrams(text: str, n: int = 3) -> set:
    """Simple character n-grams for similarity"""
    t = text.lower().strip()
    return {t[i:i+n] for i in range(len(t)-n+1)}

def similarity(a: str, b: str) -> float:
    """Jaccard similarity on trigrams"""
    na, nb = _ngrams(a), _ngrams(b)
    if not na or not nb: return 0.0
    return len(na & nb) / len(na | nb)

def find_similar(prompt: str, threshold: float = 0.85) -> dict | None:
    """Return cached response if similar prompt found"""
    prompt_hash = hashlib.md5(prompt.encode()).hexdigest()[:8]
    
    # Check recent prompts
    recent = r.lrange("jarvis:dedup:recent_prompts", 0, 49)
    for entry_raw in recent:
        try:
            entry = json.loads(entry_raw)
            sim = similarity(prompt, entry["prompt"])
            if sim >= threshold:
                r.incr("jarvis:dedup:hits")
                return {"cached_response": entry["response"], "similarity": round(sim, 3),
                        "original_prompt": entry["prompt"]}
        except:
            continue
    r.incr("jarvis:dedup:misses")
    return None

def store(prompt: str, response: str):
    entry = {"prompt": prompt[:500], "response": response[:1000],
             "ts": datetime.now().isoformat()[:19]}
    r.lpush("jarvis:dedup:recent_prompts", json.dumps(entry))
    r.ltrim("jarvis:dedup:recent_prompts", 0, 99)

def stats() -> dict:
    hits = int(r.get("jarvis:dedup:hits") or 0)
    misses = int(r.get("jarvis:dedup:misses") or 0)
    total = hits + misses
    return {"hits": hits, "misses": misses, "ratio": round(hits/total*100,1) if total else 0}

if __name__ == "__main__":
    store("What is the capital of France?", "Paris")
    r1 = find_similar("What is the capital of France?")
    r2 = find_similar("What's the capital city of France?")
    r3 = find_similar("How to make pasta?")
    print(f"Exact match: {r1['similarity'] if r1 else 'None'}")
    print(f"Near match: {r2['similarity'] if r2 else 'None'}")
    print(f"Different: {r3}")
    print(f"Stats: {stats()}")
