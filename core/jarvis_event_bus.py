#!/usr/bin/env python3
"""JARVIS Event Bus — Redis pub/sub pour orchestration événementielle"""
import redis, json, time, threading
from datetime import datetime

REDIS_HOST = "127.0.0.1"
REDIS_PORT = 6379
CHANNEL = "jarvis:events"

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

def publish(event_type: str, data: dict, source: str = "jarvis"):
    payload = json.dumps({
        "type": event_type,
        "source": source,
        "ts": datetime.utcnow().isoformat(),
        "data": data
    })
    r.publish(CHANNEL, payload)
    r.lpush("jarvis:event_log", payload)
    r.ltrim("jarvis:event_log", 0, 999)  # Keep last 1000 events

def subscribe(handler_fn, channels=None):
    if channels is None:
        channels = [CHANNEL, "jarvis:alerts", "jarvis:gpu", "jarvis:llm"]
    ps = r.pubsub()
    ps.subscribe(*channels)
    for msg in ps.listen():
        if msg["type"] == "message":
            try:
                event = json.loads(msg["data"])
                handler_fn(event)
            except Exception as e:
                print(f"[EventBus] ERR: {e}")

def get_recent_events(n=20):
    events = r.lrange("jarvis:event_log", 0, n-1)
    return [json.loads(e) for e in events]

def set_metric(key: str, value, ttl: int = 3600):
    r.setex(f"jarvis:metric:{key}", ttl, json.dumps(value))

def get_metric(key: str):
    v = r.get(f"jarvis:metric:{key}")
    return json.loads(v) if v else None

if __name__ == "__main__":
    # Test
    publish("system_ready", {"version": "1.0", "phase": "optimization"})
    events = get_recent_events(5)
    print(f"✅ EventBus OK — {len(events)} events dans log")
    for e in events[:3]:
        print(f"  [{e['ts'][:19]}] {e['type']} from {e['source']}")
