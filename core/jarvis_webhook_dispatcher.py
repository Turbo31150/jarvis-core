#!/usr/bin/env python3
"""JARVIS Webhook Dispatcher — Send outbound webhooks with retry, signing and delivery tracking"""

import redis
import json
import time
import hashlib
import hmac
import requests

r = redis.Redis(decode_responses=True)

ENDPOINT_PREFIX = "jarvis:webhook:endpoint:"
QUEUE_KEY = "jarvis:webhook:queue"
LOG_KEY = "jarvis:webhook:log"
STATS_KEY = "jarvis:webhook:stats"
INDEX_KEY = "jarvis:webhook:endpoints"


def register_endpoint(
    name: str, url: str, secret: str = "", events: list = None, timeout_s: int = 10
) -> str:
    eid = hashlib.md5(name.encode()).hexdigest()[:10]
    endpoint = {
        "id": eid,
        "name": name,
        "url": url,
        "secret": secret,
        "events": events or ["*"],
        "timeout_s": timeout_s,
        "enabled": True,
        "created_at": time.time(),
    }
    r.setex(f"{ENDPOINT_PREFIX}{eid}", 86400 * 365, json.dumps(endpoint))
    r.sadd(INDEX_KEY, eid)
    r.hincrby(STATS_KEY, "endpoints_registered", 1)
    return eid


def _sign_payload(payload: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def enqueue(event_type: str, payload: dict, priority: int = 5):
    """Add event to dispatch queue."""
    r.zadd(
        QUEUE_KEY,
        {
            json.dumps(
                {
                    "event_type": event_type,
                    "payload": payload,
                    "enqueued_at": time.time(),
                    "priority": priority,
                }
            ): priority
        },
    )
    r.hincrby(STATS_KEY, "events_enqueued", 1)


def dispatch_event(event_type: str, payload: dict) -> list:
    """Immediately dispatch to matching endpoints."""
    endpoints = _get_matching_endpoints(event_type)
    results = []
    for ep in endpoints:
        result = _send(ep, event_type, payload)
        results.append(result)
    return results


def _get_matching_endpoints(event_type: str) -> list:
    eids = r.smembers(INDEX_KEY)
    matched = []
    for eid in eids:
        raw = r.get(f"{ENDPOINT_PREFIX}{eid}")
        if not raw:
            continue
        ep = json.loads(raw)
        if not ep.get("enabled"):
            continue
        events = ep.get("events", ["*"])
        if "*" in events or event_type in events:
            matched.append(ep)
    return matched


def _send(endpoint: dict, event_type: str, payload: dict, attempt: int = 1) -> dict:
    body = json.dumps(
        {"event": event_type, "payload": payload, "ts": time.time(), "attempt": attempt}
    )
    headers = {"Content-Type": "application/json", "X-JARVIS-Event": event_type}
    if endpoint.get("secret"):
        headers["X-JARVIS-Signature"] = _sign_payload(body, endpoint["secret"])

    t0 = time.perf_counter()
    try:
        resp = requests.post(
            endpoint["url"],
            data=body,
            headers=headers,
            timeout=endpoint.get("timeout_s", 10),
        )
        lat = round((time.perf_counter() - t0) * 1000)
        success = resp.status_code < 400
        result = {
            "endpoint": endpoint["name"],
            "status": resp.status_code,
            "latency_ms": lat,
            "success": success,
            "attempt": attempt,
        }
    except Exception as e:
        lat = round((time.perf_counter() - t0) * 1000)
        result = {
            "endpoint": endpoint["name"],
            "success": False,
            "error": str(e)[:60],
            "latency_ms": lat,
            "attempt": attempt,
        }

    # Log delivery
    log_entry = {**result, "event_type": event_type, "ts": time.time()}
    r.lpush(LOG_KEY, json.dumps(log_entry))
    r.ltrim(LOG_KEY, 0, 999)
    r.hincrby(STATS_KEY, "delivered" if result["success"] else "failed", 1)
    return result


def retry_failed(max_age_s: int = 3600) -> int:
    """Re-process queued events."""
    batch = r.zrevrange(QUEUE_KEY, 0, 19, withscores=True)
    retried = 0
    for raw, _ in batch:
        event = json.loads(raw)
        age = time.time() - event.get("enqueued_at", 0)
        if age > max_age_s:
            r.zrem(QUEUE_KEY, raw)
            continue
        results = dispatch_event(event["event_type"], event["payload"])
        if all(r_["success"] for r_ in results):
            r.zrem(QUEUE_KEY, raw)
            retried += 1
    return retried


def delivery_log(limit: int = 10) -> list:
    return [json.loads(x) for x in r.lrange(LOG_KEY, 0, limit - 1)]


def stats() -> dict:
    s = r.hgetall(STATS_KEY)
    return {k: int(v) for k, v in s.items()}


if __name__ == "__main__":
    register_endpoint(
        "telegram_notif",
        "http://127.0.0.1:9090/webhook/telegram",
        secret="jarvis-secret",
        events=["alert", "health"],
    )
    register_endpoint("dashboard", "http://127.0.0.1:8767/events", events=["*"])
    register_endpoint(
        "trading_hook",
        "http://127.0.0.1:9191/trading",
        events=["trading.signal", "trading.fill"],
    )

    # Simulate dispatches (endpoints won't actually be up)
    test_events = [
        ("alert", {"severity": "critical", "msg": "GPU0 temp 89C"}),
        ("health", {"status": "degraded", "node": "m2"}),
        ("trading.signal", {"symbol": "BTC", "side": "long", "price": 84000}),
        ("unknown.type", {"data": "test"}),
    ]

    print("Dispatching events:")
    for event_type, payload in test_events:
        results = dispatch_event(event_type, payload)
        for res in results:
            icon = "✅" if res["success"] else "❌"
            err = (
                res.get("error", "")[:30]
                if not res["success"]
                else f"{res.get('status', '?')}"
            )
            print(f"  {icon} {event_type:20s} → {res['endpoint']:15s}  {err}")

    enqueue("trading.signal", {"symbol": "ETH", "price": 3200}, priority=8)
    print(f"\nQueued: {r.zcard(QUEUE_KEY)} pending events")
    print(f"Stats: {stats()}")
