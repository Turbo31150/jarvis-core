#!/usr/bin/env python3
"""JARVIS Chaos Monkey — Controlled fault injection for resilience testing"""

import redis
import json
import time
import random
from datetime import datetime

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:chaos"

EXPERIMENTS = {
    "circuit_open": {
        "description": "Artificially open a circuit breaker",
        "safe": True,
        "reversible": True,
    },
    "redis_key_delete": {
        "description": "Delete a non-critical Redis key",
        "safe": True,
        "reversible": False,
    },
    "score_corrupt": {
        "description": "Corrupt the score data to test fallback",
        "safe": True,
        "reversible": True,
    },
    "rate_limit_exhaustion": {
        "description": "Exhaust rate limit for a backend",
        "safe": True,
        "reversible": True,
    },
    "slow_response": {
        "description": "Simulate slow Redis responses via sleep",
        "safe": True,
        "reversible": True,
    },
}


def run_experiment(name: str, target: str = "m2", duration_s: int = 10) -> dict:
    if name not in EXPERIMENTS:
        return {"error": f"Unknown experiment: {name}"}

    exp = EXPERIMENTS[name]
    t0 = time.time()
    result = {"experiment": name, "target": target, "duration_s": duration_s}

    try:
        if name == "circuit_open":
            # Open circuit breaker for target
            r.hset(f"jarvis:cb:{target}", mapping={"state": "open", "failures": 5, "opened_at": time.time()})
            r.expire(f"jarvis:cb:{target}", duration_s)
            result["action"] = f"Opened circuit breaker for {target}, expires in {duration_s}s"

        elif name == "redis_key_delete":
            # Delete a safe temp key
            key = f"jarvis:temp:chaos_test_{int(time.time())}"
            r.set(key, "test_value")
            time.sleep(0.1)
            r.delete(key)
            result["action"] = f"Created and deleted temp key: {key}"

        elif name == "score_corrupt":
            # Save original, inject bad score, restore after duration_s
            original = r.get("jarvis:score")
            bad_score = json.dumps({"total": 0, "corrupted_by_chaos": True, "ts": datetime.now().isoformat()[:19]})
            r.setex("jarvis:score", duration_s, bad_score)
            result["action"] = f"Injected corrupt score for {duration_s}s"
            result["original_saved"] = bool(original)

        elif name == "rate_limit_exhaustion":
            # Fill rate limit bucket for target
            key = f"jarvis:rl:{target}:requests"
            r.setex(key, duration_s, 9999)
            result["action"] = f"Exhausted rate limit for {target} for {duration_s}s"

        elif name == "slow_response":
            # Simulate slow operation
            time.sleep(min(duration_s, 2))
            result["action"] = f"Simulated {min(duration_s, 2)}s slowdown"

        result["ok"] = True
        result["elapsed_ms"] = round((time.time() - t0) * 1000)
        result["ts"] = datetime.now().isoformat()[:19]

        # Log the experiment
        r.lpush(f"{PREFIX}:log", json.dumps(result))
        r.ltrim(f"{PREFIX}:log", 0, 99)

    except Exception as e:
        result["ok"] = False
        result["error"] = str(e)[:60]

    return result


def rollback(name: str, target: str = "m2"):
    """Manually rollback a chaos experiment"""
    if name == "circuit_open":
        r.delete(f"jarvis:cb:{target}")
        return {"rolled_back": f"circuit_breaker:{target}"}
    if name == "score_corrupt":
        r.delete("jarvis:score")
        return {"rolled_back": "jarvis:score (will be repopulated by hw-monitor)"}
    if name == "rate_limit_exhaustion":
        r.delete(f"jarvis:rl:{target}:requests")
        return {"rolled_back": f"rate_limit:{target}"}
    return {"error": "No rollback defined"}


def history(n: int = 10) -> list:
    raw = r.lrange(f"{PREFIX}:log", 0, n - 1)
    return [json.loads(e) for e in raw]


if __name__ == "__main__":
    import sys
    exp_name = sys.argv[1] if len(sys.argv) > 1 else "circuit_open"
    target = sys.argv[2] if len(sys.argv) > 2 else "test_backend"

    print(f"Running chaos experiment: {exp_name} on {target}")
    res = run_experiment(exp_name, target, duration_s=5)
    print(f"  Result: {'✅' if res.get('ok') else '❌'} {res.get('action', res.get('error', ''))}")
    print(f"  Elapsed: {res.get('elapsed_ms')}ms")

    # Auto-rollback
    rb = rollback(exp_name, target)
    print(f"  Rollback: {rb}")

    print(f"\nAvailable experiments:")
    for name, cfg in EXPERIMENTS.items():
        safe = "✅" if cfg["safe"] else "⚠️"
        rev = "↩️" if cfg["reversible"] else "💥"
        print(f"  {safe}{rev} {name}: {cfg['description']}")
