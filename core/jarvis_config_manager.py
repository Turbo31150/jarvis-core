#!/usr/bin/env python3
"""JARVIS Config Manager — Centralized config with hot-reload and validation"""

import redis
import json
import os
import time
from typing import Any

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:config"

# Default config schema
DEFAULTS = {
    "llm.default_timeout": 45,
    "llm.max_tokens": 800,
    "llm.temperature": 0.1,
    "circuit_breaker.threshold": 3,
    "circuit_breaker.timeout_s": 60,
    "rate_limiter.m1_rpm": 20,
    "rate_limiter.m2_rpm": 10,
    "rate_limiter.ol1_rpm": 30,
    "scheduler.gpu_temp_limit": 78,
    "scheduler.ram_min_gb": 4,
    "alerts.telegram_enabled": True,
    "alerts.min_severity": "warning",
    "benchmark.interval_m": 60,
    "mesh.health_interval_s": 30,
    "dashboard.refresh_s": 10,
    "score.weights.cpu_thermal": 20,
    "score.weights.ram": 20,
    "score.weights.gpu": 20,
    "score.weights.llm": 20,
    "score.weights.services": 20,
}

TYPES = {
    "llm.default_timeout": int,
    "llm.max_tokens": int,
    "llm.temperature": float,
    "circuit_breaker.threshold": int,
    "circuit_breaker.timeout_s": int,
    "rate_limiter.m1_rpm": int,
    "rate_limiter.m2_rpm": int,
    "rate_limiter.ol1_rpm": int,
    "scheduler.gpu_temp_limit": int,
    "scheduler.ram_min_gb": int,
    "alerts.telegram_enabled": bool,
    "alerts.min_severity": str,
    "benchmark.interval_m": int,
    "mesh.health_interval_s": int,
    "dashboard.refresh_s": int,
}


def _redis_key(key: str) -> str:
    return f"{PREFIX}:{key.replace('.', ':')}"


def get(key: str, default: Any = None) -> Any:
    val = r.get(_redis_key(key))
    if val is None:
        return DEFAULTS.get(key, default)
    # Cast to expected type
    expected_type = TYPES.get(key)
    if expected_type == bool:
        return val.lower() in ("true", "1", "yes")
    if expected_type == int:
        return int(val)
    if expected_type == float:
        return float(val)
    return val


def set_config(key: str, value: Any) -> bool:
    if key not in DEFAULTS:
        return False
    r.set(_redis_key(key), str(value))
    r.publish(f"{PREFIX}:changes", json.dumps({"key": key, "value": str(value), "ts": time.time()}))
    return True


def get_all() -> dict:
    result = {}
    for key, default in DEFAULTS.items():
        result[key] = get(key, default)
    return result


def reset(key: str = None):
    if key:
        r.delete(_redis_key(key))
    else:
        for k in DEFAULTS:
            r.delete(_redis_key(k))


def load_env_overrides():
    """Load config overrides from environment variables (JARVIS_CONFIG_<KEY>=val)"""
    loaded = 0
    for env_key, env_val in os.environ.items():
        if env_key.startswith("JARVIS_CONFIG_"):
            cfg_key = env_key[14:].lower().replace("_", ".")
            if cfg_key in DEFAULTS:
                set_config(cfg_key, env_val)
                loaded += 1
    return loaded


if __name__ == "__main__":
    # Show all config
    loaded = load_env_overrides()
    cfg = get_all()
    print(f"Config ({len(cfg)} keys, {loaded} env overrides):")
    prev_prefix = None
    for k, v in sorted(cfg.items()):
        prefix = k.split(".")[0]
        if prefix != prev_prefix:
            print(f"  [{prefix}]")
            prev_prefix = prefix
        print(f"    {k} = {v}")
