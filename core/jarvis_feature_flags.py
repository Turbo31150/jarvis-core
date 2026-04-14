#!/usr/bin/env python3
"""JARVIS Feature Flags — Progressive feature rollout with Redis-backed flags"""
import redis, json
from datetime import datetime

r = redis.Redis(decode_responses=True)

# Default flags (can be overridden via Redis)
DEFAULTS = {
    "use_circuit_breaker":  True,
    "use_rate_limiter":     True,
    "use_semantic_dedup":   True,
    "use_context_cache":    True,
    "use_distributed_tracer": False,  # experimental
    "use_ab_testing":       False,
    "use_cost_optimizer":   True,
    "hyperliquid_enabled":  False,    # needs HL_PRIVATE_KEY
    "m1_routing":          False,    # m1 down
    "m3_routing":          False,    # m3 down
}

def get(flag: str) -> bool:
    # Redis override takes priority
    val = r.hget("jarvis:feature_flags", flag)
    if val is not None:
        return val == "1" or val == "true"
    return DEFAULTS.get(flag, False)

def set_flag(flag: str, value: bool):
    r.hset("jarvis:feature_flags", flag, "1" if value else "0")
    r.lpush("jarvis:feature_flag_changes",
            json.dumps({"flag": flag, "value": value, "ts": datetime.now().isoformat()[:19]}))

def all_flags() -> dict:
    overrides = r.hgetall("jarvis:feature_flags")
    result = {}
    for flag, default in DEFAULTS.items():
        if flag in overrides:
            result[flag] = overrides[flag] == "1" or overrides[flag] == "true"
        else:
            result[flag] = default
    return result

def init_defaults():
    """Initialize all defaults in Redis if not already set"""
    for flag, val in DEFAULTS.items():
        if not r.hexists("jarvis:feature_flags", flag):
            r.hset("jarvis:feature_flags", flag, "1" if val else "0")

if __name__ == "__main__":
    init_defaults()
    flags = all_flags()
    print("Feature flags:")
    for flag, val in sorted(flags.items()):
        status = "✅" if val else "❌"
        print(f"  {status} {flag}")
