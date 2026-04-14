#!/usr/bin/env python3
"""JARVIS Alert Router — Route alerts to channels (Telegram, Redis, log) with dedup and throttle"""

import redis
import json
import time
import hashlib

r = redis.Redis(decode_responses=True)

QUEUE_KEY = "jarvis:alert_router:queue"
SENT_PREFIX = "jarvis:alert_router:sent:"
STATS_KEY = "jarvis:alert_router:stats"
THROTTLE_PREFIX = "jarvis:alert_router:throttle:"

CHANNELS = {
    "telegram": {"enabled": True, "min_severity": "warn"},
    "redis_pub": {"enabled": True, "min_severity": "info"},
    "log": {"enabled": True, "min_severity": "debug"},
}

SEVERITY_ORDER = {"debug": 0, "info": 1, "warn": 2, "error": 3, "critical": 4}

THROTTLE_WINDOWS = {
    "warn": 300,  # same alert max once per 5min
    "error": 120,
    "critical": 30,
    "info": 600,
}


def _dedup_key(source: str, msg: str) -> str:
    return hashlib.md5(f"{source}:{msg[:100]}".encode()).hexdigest()[:16]


def _is_throttled(source: str, msg: str, severity: str) -> bool:
    key = f"{THROTTLE_PREFIX}{_dedup_key(source, msg)}"
    if r.exists(key):
        return True
    ttl = THROTTLE_WINDOWS.get(severity, 300)
    r.setex(key, ttl, "1")
    return False


def _route_to_channel(channel: str, alert: dict) -> bool:
    cfg = CHANNELS.get(channel, {})
    if not cfg.get("enabled"):
        return False
    min_sev = SEVERITY_ORDER.get(cfg.get("min_severity", "info"), 1)
    if SEVERITY_ORDER.get(alert["severity"], 1) < min_sev:
        return False

    if channel == "redis_pub":
        r.publish("jarvis:alerts", json.dumps(alert))
        return True
    elif channel == "log":
        r.lpush("jarvis:alert_log", json.dumps(alert))
        r.ltrim("jarvis:alert_log", 0, 999)
        return True
    elif channel == "telegram":
        # Queue for Telegram bot pickup
        r.lpush(
            "jarvis:telegram:queue",
            json.dumps(
                {
                    "text": f"[{alert['severity'].upper()}] {alert['source']}: {alert['message']}",
                    "ts": alert["ts"],
                }
            ),
        )
        r.ltrim("jarvis:telegram:queue", 0, 99)
        return True
    return False


def send(
    source: str,
    message: str,
    severity: str = "info",
    tags: list = None,
    deduplicate: bool = True,
) -> dict:
    """Route an alert to all applicable channels."""
    if deduplicate and _is_throttled(source, message, severity):
        r.hincrby(STATS_KEY, "throttled", 1)
        return {"sent": False, "reason": "throttled"}

    alert = {
        "source": source,
        "message": message,
        "severity": severity,
        "tags": tags or [],
        "ts": time.time(),
    }
    sent_to = []
    for channel in CHANNELS:
        if _route_to_channel(channel, alert):
            sent_to.append(channel)

    r.hincrby(STATS_KEY, f"sent:{severity}", 1)
    r.hincrby(STATS_KEY, "total_sent", 1)
    return {"sent": True, "channels": sent_to, "severity": severity}


def get_log(limit: int = 20, min_severity: str = "info") -> list:
    min_level = SEVERITY_ORDER.get(min_severity, 1)
    raw = r.lrange("jarvis:alert_log", 0, limit * 2)
    results = []
    for item in raw:
        alert = json.loads(item)
        if SEVERITY_ORDER.get(alert.get("severity", "info"), 1) >= min_level:
            results.append(alert)
        if len(results) >= limit:
            break
    return results


def stats() -> dict:
    s = r.hgetall(STATS_KEY)
    return {k: int(v) for k, v in s.items()}


if __name__ == "__main__":
    alerts = [
        ("gpu_monitor", "GPU0 temperature 79°C — approaching limit", "warn", ["gpu"]),
        ("llm_router", "M2 timeout after 45s", "error", ["llm", "m2"]),
        ("hw_monitor", "CPU usage 45%", "info", ["system"]),
        ("llm_router", "M2 timeout after 45s", "error", ["llm"]),  # dedup
        ("circuit_breaker", "M2 circuit opened after 5 failures", "critical", ["m2"]),
        ("scheduler", "All tasks processed", "debug", []),
    ]
    for source, msg, sev, tags in alerts:
        result = send(source, msg, sev, tags)
        icon = "📤" if result["sent"] else "🔇"
        channels = result.get("channels", [])
        reason = result.get("reason", "")
        print(f"  {icon} [{sev:8s}] {source}: {channels or reason}")

    print(f"\nLog (warn+): {len(get_log(min_severity='warn'))} entries")
    print(f"Stats: {stats()}")
