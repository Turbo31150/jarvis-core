#!/usr/bin/env python3
"""JARVIS Multi-Tenant — Tenant isolation for API keys, quotas, and namespaces"""

import redis
import json
import uuid
import hashlib
import time
from datetime import datetime

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:tenants"

DEFAULT_TENANT_LIMITS = {
    "requests_per_min": 60,
    "llm_tokens_per_day": 100000,
    "max_sessions": 10,
    "allowed_backends": ["ol1", "m2"],
    "tier": "standard",
}

TIER_LIMITS = {
    "free":       {"requests_per_min": 10,  "llm_tokens_per_day": 10000,  "tier": "free"},
    "standard":   {"requests_per_min": 60,  "llm_tokens_per_day": 100000, "tier": "standard"},
    "premium":    {"requests_per_min": 300, "llm_tokens_per_day": 500000, "tier": "premium"},
    "admin":      {"requests_per_min": 1000,"llm_tokens_per_day": -1,     "tier": "admin"},
}


def create_tenant(name: str, tier: str = "standard", metadata: dict = None) -> dict:
    tenant_id = f"t_{uuid.uuid4().hex[:8]}"
    api_key = f"jk_{hashlib.sha256(f'{tenant_id}{time.time()}'.encode()).hexdigest()[:24]}"
    limits = {**DEFAULT_TENANT_LIMITS, **TIER_LIMITS.get(tier, {})}
    tenant = {
        "id": tenant_id,
        "name": name,
        "api_key": api_key,
        "tier": tier,
        "limits": json.dumps(limits),
        "created_at": datetime.now().isoformat()[:19],
        "active": "true",
    }
    r.hset(f"{PREFIX}:{tenant_id}", mapping=tenant)
    r.hset(f"{PREFIX}:key_index", api_key, tenant_id)
    return {"id": tenant_id, "name": name, "api_key": api_key, "tier": tier, "limits": limits}


def get_tenant_by_key(api_key: str) -> dict | None:
    tenant_id = r.hget(f"{PREFIX}:key_index", api_key)
    if not tenant_id:
        return None
    data = r.hgetall(f"{PREFIX}:{tenant_id}")
    if not data or data.get("active") != "true":
        return None
    data["limits"] = json.loads(data.get("limits", "{}"))
    return data


def check_tenant_quota(tenant_id: str, resource: str = "requests") -> dict:
    """Check if tenant is within quota for a resource"""
    data = r.hgetall(f"{PREFIX}:{tenant_id}")
    if not data:
        return {"allowed": False, "reason": "tenant_not_found"}
    limits = json.loads(data.get("limits", "{}"))

    if resource == "requests":
        limit = limits.get("requests_per_min", 60)
        key = f"{PREFIX}:usage:{tenant_id}:requests:{int(time.time() // 60)}"
        count = int(r.incr(key))
        r.expire(key, 120)
        allowed = count <= limit
        return {"allowed": allowed, "count": count, "limit": limit, "resource": resource}

    if resource == "tokens":
        today = datetime.now().strftime("%Y-%m-%d")
        limit = limits.get("llm_tokens_per_day", 100000)
        if limit == -1:
            return {"allowed": True, "unlimited": True}
        key = f"{PREFIX}:usage:{tenant_id}:tokens:{today}"
        used = int(r.get(key) or 0)
        return {"allowed": used < limit, "used": used, "limit": limit, "resource": resource}

    return {"allowed": True, "resource": resource}


def record_token_usage(tenant_id: str, tokens: int):
    today = datetime.now().strftime("%Y-%m-%d")
    key = f"{PREFIX}:usage:{tenant_id}:tokens:{today}"
    r.incrby(key, tokens)
    r.expire(key, 86400 * 2)


def list_tenants() -> list:
    tenants = []
    for key in r.scan_iter(f"{PREFIX}:t_*"):
        if ":usage:" in key or ":key_index" in key:
            continue
        data = r.hgetall(key)
        if data:
            tenants.append({"id": data.get("id"), "name": data.get("name"), "tier": data.get("tier"), "active": data.get("active") == "true"})
    return tenants


if __name__ == "__main__":
    # Create test tenants
    t1 = create_tenant("JARVIS-Admin", "admin")
    t2 = create_tenant("Test-User", "free")
    t3 = create_tenant("Premium-Client", "premium")

    print(f"Tenants created:")
    print(f"  Admin: id={t1['id']} key={t1['api_key'][:12]}...")
    print(f"  Free:  id={t2['id']} key={t2['api_key'][:12]}...")

    # Test lookup by key
    found = get_tenant_by_key(t1["api_key"])
    print(f"\nLookup by API key: {found['name']} ({found['tier']})")

    # Test quota
    for i in range(12):
        q = check_tenant_quota(t2["id"], "requests")
    print(f"\nFree tier quota check (limit=10): allowed={q['allowed']}, count={q['count']}")

    print(f"\nAll tenants: {list_tenants()}")
