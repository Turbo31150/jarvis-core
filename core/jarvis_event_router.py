#!/usr/bin/env python3
"""JARVIS Event Router — Route events to handlers based on type/pattern matching"""

import redis
import json
import time
import re
import hashlib

r = redis.Redis(decode_responses=True)

ROUTE_PREFIX = "jarvis:evrouter:route:"
INDEX_KEY = "jarvis:evrouter:routes"
LOG_KEY = "jarvis:evrouter:log"
STATS_KEY = "jarvis:evrouter:stats"
DEAD_LETTER = "jarvis:evrouter:dead"


def register_route(
    event_type: str,
    pattern: str,
    handler: str,
    priority: int = 5,
    filter_expr: dict = None,
) -> str:
    rid = hashlib.md5(f"{event_type}:{handler}".encode()).hexdigest()[:10]
    route = {
        "id": rid,
        "event_type": event_type,
        "pattern": pattern,
        "handler": handler,
        "priority": priority,
        "filter": filter_expr or {},
        "enabled": True,
        "created_at": time.time(),
        "match_count": 0,
    }
    r.hset(ROUTE_PREFIX + event_type, rid, json.dumps(route))
    r.sadd(INDEX_KEY, f"{event_type}:{rid}")
    r.hincrby(STATS_KEY, "routes_registered", 1)
    return rid


def _matches(event: dict, route: dict) -> bool:
    pattern = route.get("pattern", "*")
    if pattern != "*":
        subject = event.get("subject", event.get("type", ""))
        if not re.search(pattern, subject):
            return False
    for field, value in route.get("filter", {}).items():
        if event.get(field) != value:
            return False
    return True


def route_event(event: dict) -> list:
    """Route an event, return list of matched handlers."""
    event_type = event.get("type", "unknown")
    handlers = []

    # Get routes for this type + wildcard
    for etype in [event_type, "*"]:
        raw_routes = r.hvals(ROUTE_PREFIX + etype)
        for raw in raw_routes:
            route = json.loads(raw)
            if not route.get("enabled"):
                continue
            if _matches(event, route):
                handlers.append(route)

    handlers.sort(key=lambda x: -x.get("priority", 5))

    log_entry = {
        "event_type": event_type,
        "subject": event.get("subject", ""),
        "handlers": [h["handler"] for h in handlers],
        "ts": time.time(),
    }
    r.lpush(LOG_KEY, json.dumps(log_entry))
    r.ltrim(LOG_KEY, 0, 499)

    if not handlers:
        r.lpush(DEAD_LETTER, json.dumps({**event, "ts": time.time()}))
        r.ltrim(DEAD_LETTER, 0, 99)
        r.hincrby(STATS_KEY, "dead_letter", 1)

    # Update match counts
    for h in handlers:
        h["match_count"] = h.get("match_count", 0) + 1
        r.hset(ROUTE_PREFIX + event_type, h["id"], json.dumps(h))

    r.hincrby(STATS_KEY, "events_routed", 1)
    return [h["handler"] for h in handlers]


def disable_route(event_type: str, rid: str):
    raw = r.hget(ROUTE_PREFIX + event_type, rid)
    if raw:
        route = json.loads(raw)
        route["enabled"] = False
        r.hset(ROUTE_PREFIX + event_type, rid, json.dumps(route))


def list_routes(event_type: str = None) -> list:
    results = []
    if event_type:
        for raw in r.hvals(ROUTE_PREFIX + event_type):
            results.append(json.loads(raw))
    else:
        for key in r.scan_iter(ROUTE_PREFIX + "*"):
            for raw in r.hvals(key):
                results.append(json.loads(raw))
    return results


def recent_log(limit: int = 10) -> list:
    return [json.loads(x) for x in r.lrange(LOG_KEY, 0, limit - 1)]


def stats() -> dict:
    s = r.hgetall(STATS_KEY)
    return {k: int(v) for k, v in s.items()}


if __name__ == "__main__":
    # Register routes
    register_route("gpu.alert", r"temp|thermal", "thermal_guard", priority=10)
    register_route("gpu.alert", r"vram|memory", "vram_manager", priority=9)
    register_route("llm.error", r".*", "circuit_breaker", priority=8)
    register_route("llm.error", r"timeout", "retry_handler", priority=7)
    register_route("cluster.health", r".*", "health_dashboard", priority=6)
    register_route("*", r".*", "audit_logger", priority=1)  # catch-all

    events = [
        {"type": "gpu.alert", "subject": "GPU0 temp 89C", "severity": "critical"},
        {"type": "gpu.alert", "subject": "GPU1 vram 95%", "severity": "warn"},
        {"type": "llm.error", "subject": "M2 timeout after 45s", "backend": "m2"},
        {"type": "cluster.health", "subject": "M32 degraded"},
        {"type": "unknown.event", "subject": "mystery"},
    ]

    print("Event Routing:")
    for ev in events:
        handlers = route_event(ev)
        print(f"  {ev['type']:20s} '{ev['subject'][:30]:30s}' → {handlers}")

    print(f"\nDead letter: {r.llen(DEAD_LETTER)} events")
    print(f"Stats: {stats()}")
