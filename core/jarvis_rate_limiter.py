#!/usr/bin/env python3
"""JARVIS Rate Limiter — Token bucket rate limiting per LLM backend"""
import redis, time

r = redis.Redis(decode_responses=True)

# requests/minute per backend
LIMITS = {
    "m1": 20,
    "m2": 10,  # more limited
    "ol1": 30,
    "gemini": 60,
}

def _bucket_key(backend: str) -> str:
    return f"jarvis:ratelimit:{backend}"

def check_and_consume(backend: str) -> bool:
    """Returns True if request allowed, False if rate limited"""
    limit = LIMITS.get(backend, 20)
    key = _bucket_key(backend)
    pipe = r.pipeline()
    now = time.time()
    window = 60  # 1 minute window
    
    # Sliding window counter
    pipe.zremrangebyscore(key, 0, now - window)
    pipe.zadd(key, {str(now): now})
    pipe.zcard(key)
    pipe.expire(key, window + 1)
    results = pipe.execute()
    count = results[2]
    
    if count > limit:
        r.incr(f"jarvis:ratelimit:{backend}:blocked")
        return False
    return True

def stats() -> dict:
    res = {}
    for backend in LIMITS:
        key = _bucket_key(backend)
        current = r.zcard(key)
        blocked = r.get(f"jarvis:ratelimit:{backend}:blocked") or "0"
        res[backend] = {"current_rpm": current, "limit": LIMITS[backend], "blocked_total": int(blocked)}
    return res

if __name__ == "__main__":
    for _ in range(5):
        ok = check_and_consume("m2")
        print(f"m2 allowed: {ok}")
    print("Stats:", stats())
