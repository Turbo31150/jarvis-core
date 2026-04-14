#!/usr/bin/env python3
"""JARVIS API Key Manager — Manage and rotate API keys/secrets securely in Redis"""

import redis
import json
import time
import hashlib

r = redis.Redis(decode_responses=True)

KEY_PREFIX = "jarvis:apikey:"
INDEX_KEY = "jarvis:apikey:index"
AUDIT_KEY = "jarvis:apikey:audit"
STATS_KEY = "jarvis:apikey:stats"


def _mask(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return value[:4] + "***" + value[-4:]


def _hash_value(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def store_key(
    name: str,
    value: str,
    service: str = "default",
    ttl_days: int = None,
    tags: list = None,
) -> str:
    kid = hashlib.md5(f"{service}:{name}".encode()).hexdigest()[:12]
    entry = {
        "id": kid,
        "name": name,
        "service": service,
        "value": value,
        "value_hash": _hash_value(value),
        "tags": tags or [],
        "created_at": time.time(),
        "last_used": None,
        "use_count": 0,
    }
    ttl = int(ttl_days * 86400) if ttl_days else 86400 * 365
    r.setex(f"{KEY_PREFIX}{kid}", ttl, json.dumps(entry))
    r.sadd(INDEX_KEY, kid)
    r.hincrby(STATS_KEY, "keys_stored", 1)
    _audit(kid, "store", name, service)
    return kid


def get_key(name: str, service: str = "default") -> str | None:
    kid = hashlib.md5(f"{service}:{name}".encode()).hexdigest()[:12]
    raw = r.get(f"{KEY_PREFIX}{kid}")
    if not raw:
        return None
    entry = json.loads(raw)
    entry["last_used"] = time.time()
    entry["use_count"] = entry.get("use_count", 0) + 1
    r.setex(f"{KEY_PREFIX}{kid}", r.ttl(f"{KEY_PREFIX}{kid}"), json.dumps(entry))
    _audit(kid, "get", name, service)
    r.hincrby(STATS_KEY, "keys_retrieved", 1)
    return entry["value"]


def rotate_key(name: str, new_value: str, service: str = "default") -> bool:
    kid = hashlib.md5(f"{service}:{name}".encode()).hexdigest()[:12]
    raw = r.get(f"{KEY_PREFIX}{kid}")
    if not raw:
        return False
    entry = json.loads(raw)
    entry["value"] = new_value
    entry["value_hash"] = _hash_value(new_value)
    entry["rotated_at"] = time.time()
    entry["rotation_count"] = entry.get("rotation_count", 0) + 1
    ttl = r.ttl(f"{KEY_PREFIX}{kid}")
    r.setex(f"{KEY_PREFIX}{kid}", max(ttl, 3600), json.dumps(entry))
    _audit(kid, "rotate", name, service)
    r.hincrby(STATS_KEY, "keys_rotated", 1)
    return True


def list_keys(service: str = None) -> list:
    kids = r.smembers(INDEX_KEY)
    result = []
    for kid in kids:
        raw = r.get(f"{KEY_PREFIX}{kid}")
        if raw:
            entry = json.loads(raw)
            if service is None or entry.get("service") == service:
                safe = dict(entry)
                safe["value"] = _mask(entry["value"])
                result.append(safe)
    return result


def delete_key(name: str, service: str = "default") -> bool:
    kid = hashlib.md5(f"{service}:{name}".encode()).hexdigest()[:12]
    r.delete(f"{KEY_PREFIX}{kid}")
    r.srem(INDEX_KEY, kid)
    _audit(kid, "delete", name, service)
    r.hincrby(STATS_KEY, "keys_deleted", 1)
    return True


def _audit(kid: str, action: str, name: str, service: str):
    r.lpush(
        AUDIT_KEY,
        json.dumps(
            {
                "kid": kid,
                "action": action,
                "name": name,
                "service": service,
                "ts": time.time(),
            }
        ),
    )
    r.ltrim(AUDIT_KEY, 0, 499)


def get_audit_log(limit: int = 20) -> list:
    return [json.loads(x) for x in r.lrange(AUDIT_KEY, 0, limit - 1)]


def stats() -> dict:
    s = r.hgetall(STATS_KEY)
    return {"total_keys": r.scard(INDEX_KEY), **{k: int(v) for k, v in s.items()}}


if __name__ == "__main__":
    store_key("openai_api", "sk-abc123xyz789DEF456", service="openai", ttl_days=90)
    store_key("telegram_bot", "1234567890:AABBccDD-EEFFggHH", service="telegram")
    store_key(
        "hyperliquid_pk",
        "0x87BAC8e6ff_FAKE_KEY_TEST",
        service="trading",
        tags=["critical"],
    )

    val = get_key("openai_api", "openai")
    print(f"Retrieved openai_api: {_mask(val)}")

    rotate_key("openai_api", "sk-NEW_rotated_key_XYZ789", "openai")
    print("Rotated openai_api ✅")

    print("\nAll keys:")
    for k in list_keys():
        print(
            f"  [{k['service']:10s}] {k['name']:20s} {k['value']}  uses={k['use_count']}"
        )

    print("\nAudit log (last 5):")
    for log in get_audit_log(5):
        print(f"  {log['action']:8s} {log['name']} ({log['service']})")

    print(f"\nStats: {stats()}")
