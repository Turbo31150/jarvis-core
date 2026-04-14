#!/usr/bin/env python3
"""JARVIS Memory Store — Persistent key-value memory with TTL and namespaces"""

import redis
import json
import time
from datetime import datetime

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:mem"

NAMESPACES = {
    "user":     86400 * 30,   # 30 days
    "session":  3600,          # 1 hour
    "task":     86400,         # 1 day
    "agent":    86400 * 7,    # 7 days
    "system":   86400 * 365,  # 1 year (permanent-ish)
    "cache":    3600,          # 1 hour
}


def store(key: str, value, namespace: str = "agent", ttl: int = None, tags: list = None) -> bool:
    ns_ttl = ttl or NAMESPACES.get(namespace, 86400)
    full_key = f"{PREFIX}:{namespace}:{key}"
    entry = {
        "value": value,
        "stored_at": datetime.now().isoformat()[:19],
        "namespace": namespace,
        "tags": tags or [],
    }
    r.setex(full_key, ns_ttl, json.dumps(entry))
    # Tag index
    for tag in (tags or []):
        r.sadd(f"{PREFIX}:tags:{tag}", full_key)
        r.expire(f"{PREFIX}:tags:{tag}", ns_ttl)
    r.hincrby(f"{PREFIX}:stats", "stored", 1)
    return True


def recall(key: str, namespace: str = "agent"):
    full_key = f"{PREFIX}:{namespace}:{key}"
    raw = r.get(full_key)
    if not raw:
        return None
    r.hincrby(f"{PREFIX}:stats", "recalls", 1)
    entry = json.loads(raw)
    return entry["value"]


def recall_by_tag(tag: str) -> dict:
    keys = r.smembers(f"{PREFIX}:tags:{tag}")
    result = {}
    for full_key in keys:
        raw = r.get(full_key)
        if raw:
            entry = json.loads(raw)
            short_key = full_key.replace(f"{PREFIX}:{entry['namespace']}:", "")
            result[short_key] = entry["value"]
    return result


def forget(key: str, namespace: str = "agent") -> bool:
    full_key = f"{PREFIX}:{namespace}:{key}"
    return bool(r.delete(full_key))


def list_keys(namespace: str = "agent", pattern: str = "*") -> list:
    return [k.replace(f"{PREFIX}:{namespace}:", "") for k in r.scan_iter(f"{PREFIX}:{namespace}:{pattern}")]


def stats() -> dict:
    data = r.hgetall(f"{PREFIX}:stats")
    total = sum(1 for _ in r.scan_iter(f"{PREFIX}:*:*"))
    by_ns = {}
    for ns in NAMESPACES:
        count = sum(1 for _ in r.scan_iter(f"{PREFIX}:{ns}:*"))
        if count > 0:
            by_ns[ns] = count
    return {
        "total_entries": total,
        "by_namespace": by_ns,
        "stored": int(data.get("stored", 0)),
        "recalls": int(data.get("recalls", 0)),
    }


if __name__ == "__main__":
    # Test
    store("last_task", "benchmark M32", "agent", tags=["task", "m32"])
    store("cluster_state", {"m2": "up", "m32": "up", "ol1": "up"}, "system")
    store("user_pref_model", "m32_mistral", "user", tags=["preferences"])

    val1 = recall("last_task", "agent")
    val2 = recall("cluster_state", "system")
    print(f"Recalled 'last_task': {val1}")
    print(f"Recalled 'cluster_state': {val2}")
    by_tag = recall_by_tag("task")
    print(f"By tag 'task': {by_tag}")
    print(f"Stats: {stats()}")
