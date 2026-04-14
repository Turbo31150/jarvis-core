#!/usr/bin/env python3
"""JARVIS Alert Aggregator — Deduplicate and rate-limit alerts to prevent storm"""
import redis, json, time
from datetime import datetime

r = redis.Redis(decode_responses=True)

COOLDOWNS = {
    "gpu_thermal_warning": 300,   # max 1 alert per 5min per GPU
    "ram_critical":        120,
    "node_down":           600,
    "circuit_open":        180,
    "disk_full":          1800,
    "default":              60,
}

def should_send(alert_type: str, key: str = "") -> bool:
    """Returns True if alert should be forwarded (not in cooldown)"""
    cooldown = COOLDOWNS.get(alert_type, COOLDOWNS["default"])
    redis_key = f"jarvis:alert_cd:{alert_type}:{key}"
    if r.exists(redis_key):
        return False
    r.setex(redis_key, cooldown, "1")
    r.incr(f"jarvis:alert_stats:{alert_type}")
    return True

def process_event(event: dict) -> bool:
    """Process incoming event, return True if should be forwarded"""
    etype = event.get("type", "")
    data = event.get("data", {})
    
    # Dedup key based on event type + relevant data field
    if "gpu" in data:
        key = str(data["gpu"])
    elif "service" in data:
        key = str(data["service"])
    elif "node" in data:
        key = str(data["node"])
    else:
        key = ""
    
    return should_send(etype, key)

def stats() -> dict:
    res = {}
    for key in r.scan_iter("jarvis:alert_stats:*"):
        alert_type = key.replace("jarvis:alert_stats:", "")
        res[alert_type] = int(r.get(key) or 0)
    return res

if __name__ == "__main__":
    # Test: simulate 5 GPU alerts in rapid succession
    for i in range(5):
        evt = {"type": "gpu_thermal_warning", "data": {"gpu": 0, "temp": 85}}
        ok = process_event(evt)
        print(f"  Alert {i+1}: {'SEND' if ok else 'SUPPRESSED'}")
    print("Stats:", stats())
