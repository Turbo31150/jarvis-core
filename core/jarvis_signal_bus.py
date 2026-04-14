#!/usr/bin/env python3
"""JARVIS Signal Bus — Typed event signals with priority, filters, and async dispatch"""

import redis
import json
import time

r = redis.Redis(decode_responses=True)

BUS_STREAM = "jarvis:signal_bus"
REGISTRY_KEY = "jarvis:signal_bus:registry"
STATS_KEY = "jarvis:signal_bus:stats"

SIGNAL_TYPES = {
    "alert": {"priority": 10, "ttl": 300},
    "metric": {"priority": 3, "ttl": 60},
    "command": {"priority": 8, "ttl": 120},
    "heartbeat": {"priority": 1, "ttl": 30},
    "state_change": {"priority": 7, "ttl": 600},
    "anomaly": {"priority": 9, "ttl": 300},
    "info": {"priority": 2, "ttl": 120},
}


def emit(signal_type: str, source: str, payload: dict, tags: list = None) -> str:
    """Emit a signal onto the bus."""
    cfg = SIGNAL_TYPES.get(signal_type, {"priority": 5, "ttl": 120})
    msg = {
        "type": signal_type,
        "source": source,
        "payload": json.dumps(payload),
        "tags": json.dumps(tags or []),
        "priority": cfg["priority"],
        "ts": str(time.time()),
    }
    sid = r.xadd(BUS_STREAM, msg, maxlen=1000)
    r.hincrby(STATS_KEY, f"emit:{signal_type}", 1)
    return sid


def read(
    count: int = 20, signal_type: str = None, min_priority: int = 0, last_id: str = "0"
) -> list:
    """Read signals from bus with optional filters."""
    raw = r.xrange(BUS_STREAM, last_id, "+", count=count * 3)
    results = []
    for sid, fields in raw:
        if signal_type and fields.get("type") != signal_type:
            continue
        if int(fields.get("priority", 0)) < min_priority:
            continue
        results.append(
            {
                "id": sid,
                "type": fields["type"],
                "source": fields["source"],
                "payload": json.loads(fields["payload"]),
                "tags": json.loads(fields.get("tags", "[]")),
                "priority": int(fields["priority"]),
                "ts": float(fields["ts"]),
            }
        )
        if len(results) >= count:
            break
    return results


def read_latest(count: int = 10) -> list:
    """Read most recent signals."""
    raw = r.xrevrange(BUS_STREAM, "+", "-", count=count)
    results = []
    for sid, fields in raw:
        results.append(
            {
                "id": sid,
                "type": fields["type"],
                "source": fields["source"],
                "payload": json.loads(fields["payload"]),
                "priority": int(fields["priority"]),
            }
        )
    return results


def register_handler(component: str, signal_types: list, description: str = ""):
    """Register a component as a handler for certain signal types."""
    entry = {
        "component": component,
        "types": signal_types,
        "description": description,
        "registered_at": time.time(),
    }
    r.hset(REGISTRY_KEY, component, json.dumps(entry))


def stats() -> dict:
    s = r.hgetall(STATS_KEY)
    length = r.xlen(BUS_STREAM)
    handlers = r.hlen(REGISTRY_KEY)
    by_type = {
        k.replace("emit:", ""): int(v) for k, v in s.items() if k.startswith("emit:")
    }
    return {
        "stream_length": length,
        "registered_handlers": handlers,
        "emitted_by_type": by_type,
    }


if __name__ == "__main__":
    # Register handlers
    register_handler("llm_router", ["command", "state_change"], "Routes LLM requests")
    register_handler("anomaly_scorer", ["metric", "anomaly"], "Detects anomalies")
    register_handler("alert_aggregator", ["alert", "anomaly"], "Aggregates alerts")

    # Emit various signals
    signals = [
        (
            "alert",
            "gpu_monitor",
            {"msg": "GPU0 temp 82°C", "level": "warn"},
            ["gpu", "thermal"],
        ),
        ("metric", "hw_monitor", {"cpu_pct": 45, "ram_pct": 42}, ["system"]),
        ("command", "scheduler", {"action": "scale_up", "target": "m32"}, ["infra"]),
        ("anomaly", "anomaly_scorer", {"score": 8.5, "metric": "latency_m2"}, ["ml"]),
        ("heartbeat", "watchdog", {"alive": True}, []),
    ]
    for sig_type, source, payload, tags in signals:
        sid = emit(sig_type, source, payload, tags)
        print(f"  [{sig_type:12s}] from {source}: {sid}")

    latest = read_latest(3)
    print(f"\nLatest {len(latest)} signals:")
    for s in latest:
        print(f"  p={s['priority']} [{s['type']}] {s['source']}: {s['payload']}")

    print(f"\nStats: {stats()}")
