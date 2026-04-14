#!/usr/bin/env python3
"""JARVIS Redis Streams — Persistent event bus using Redis Streams (vs pub/sub)"""
import redis, json, time
from datetime import datetime

r = redis.Redis(decode_responses=True)

STREAM = "jarvis:stream"
MAX_LEN = 5000

def publish(event_type: str, data: dict, source: str = "system", severity: str = "info"):
    payload = {
        "type": event_type, "source": source, "severity": severity,
        "data": json.dumps(data), "ts": datetime.now().isoformat()[:19]
    }
    msg_id = r.xadd(STREAM, payload, maxlen=MAX_LEN, approximate=True)
    # Also backward compat with pub/sub
    r.publish("jarvis:events", json.dumps({**payload, "data": data}))
    return msg_id

def consume(group: str, consumer: str, count: int = 10, block_ms: int = 1000):
    """Consumer group-based consumption — each message delivered once per group"""
    try:
        r.xgroup_create(STREAM, group, id="0", mkstream=True)
    except redis.ResponseError:
        pass  # group already exists
    msgs = r.xreadgroup(group, consumer, {STREAM: ">"}, count=count, block=block_ms)
    if not msgs:
        return []
    results = []
    for stream_name, messages in msgs:
        for msg_id, fields in messages:
            try:
                fields["data"] = json.loads(fields.get("data", "{}"))
            except:
                pass
            results.append({"id": msg_id, **fields})
            r.xack(STREAM, group, msg_id)
    return results

def history(count: int = 20) -> list:
    msgs = r.xrevrange(STREAM, count=count)
    return [{"id": m[0], **m[1]} for m in msgs]

def lag(group: str) -> int:
    try:
        info = r.xinfo_groups(STREAM)
        for g in info:
            if g["name"] == group:
                return g.get("lag", 0)
    except:
        pass
    return -1

if __name__ == "__main__":
    mid = publish("test_event", {"msg": "streams ok"}, "test")
    print(f"Published: {mid}")
    msgs = consume("test_group", "worker1", block_ms=100)
    print(f"Consumed: {len(msgs)} messages")
    print("History:", len(history()))
