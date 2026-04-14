#!/usr/bin/env python3
"""JARVIS Event Sourcing — Immutable event log + dead letter stream + replay"""
import redis, json, time
from datetime import datetime

r = redis.Redis(decode_responses=True)

MAIN_STREAM = "jarvis:stream"
DEAD_LETTER = "jarvis:dead_letter"
AUDIT_STREAM = "jarvis:audit"

SCHEMA = {
    "gpu_thermal_warning": ["gpu", "temp"],
    "gpu_health_issue":    ["gpu", "issues"],
    "circuit_open":        ["service", "failures"],
    "node_down":           ["node"],
    "ram_critical":        ["free_gb"],
}

def emit(event_type: str, data: dict, source: str = "system") -> str:
    """Emit validated, immutable event"""
    required = SCHEMA.get(event_type, [])
    missing = [f for f in required if f not in data]
    
    if missing:
        # Route to dead letter
        r.xadd(DEAD_LETTER, {
            "type": event_type, "data": json.dumps(data),
            "reason": f"missing_fields:{missing}", "ts": datetime.now().isoformat()[:19]
        }, maxlen=500)
        return "dead_letter"
    
    payload = {"type": event_type, "source": source, "data": json.dumps(data),
               "version": "1", "ts": datetime.now().isoformat()[:19]}
    msg_id = r.xadd(MAIN_STREAM, payload, maxlen=5000)
    
    # Audit log (all events, never trimmed beyond 10k)
    r.xadd(AUDIT_STREAM, payload, maxlen=10000)
    return msg_id

def replay(from_ts: str = "-", to_ts: str = "+", event_types: list = None) -> list:
    """Replay events — for debugging or rebuilding state"""
    msgs = r.xrange(AUDIT_STREAM, from_ts, to_ts)
    events = []
    for msg_id, fields in msgs:
        if event_types and fields.get("type") not in event_types:
            continue
        try:
            fields["data"] = json.loads(fields.get("data", "{}"))
        except: pass
        events.append({"id": msg_id, **fields})
    return events

def dead_letter_stats() -> dict:
    return {"count": r.xlen(DEAD_LETTER),
            "recent": [m[1] for m in r.xrevrange(DEAD_LETTER, count=3)]}

if __name__ == "__main__":
    # Test valid event
    mid = emit("gpu_thermal_warning", {"gpu": 0, "temp": 85})
    print(f"Valid: {mid}")
    # Test invalid (missing fields) → dead letter
    mid2 = emit("gpu_health_issue", {"gpu": 0})  # missing 'issues'
    print(f"Invalid → {mid2}")
    print("Dead letter:", dead_letter_stats()["count"])
    print("Replay 3:", len(replay(event_types=["gpu_thermal_warning"])))
