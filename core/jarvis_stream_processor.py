#!/usr/bin/env python3
"""JARVIS Stream Processor — Process Redis Streams with consumer groups"""

import redis
import json
import time
import threading
from datetime import datetime

r = redis.Redis(decode_responses=True)
STREAM_KEY = "jarvis:stream"
DLQ_KEY = "jarvis:stream:dlq"


def ensure_group(group: str, stream: str = STREAM_KEY):
    try:
        r.xgroup_create(stream, group, "$", mkstream=True)
    except redis.exceptions.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


def publish(event_type: str, data: dict, severity: str = "info") -> str:
    entry = {
        "type": event_type,
        "severity": severity,
        "data": json.dumps(data),
        "ts": datetime.now().isoformat()[:23],
    }
    msg_id = r.xadd(STREAM_KEY, entry, maxlen=5000)
    return msg_id


def consume(group: str, consumer: str, count: int = 10, block_ms: int = 100) -> list:
    ensure_group(group)
    try:
        messages = r.xreadgroup(group, consumer, {STREAM_KEY: ">"}, count=count, block=block_ms)
        if not messages:
            return []
        result = []
        for stream_name, entries in messages:
            for msg_id, fields in entries:
                try:
                    data = json.loads(fields.get("data", "{}"))
                    result.append({
                        "id": msg_id,
                        "type": fields.get("type", "unknown"),
                        "severity": fields.get("severity", "info"),
                        "data": data,
                        "ts": fields.get("ts", ""),
                    })
                except Exception:
                    result.append({"id": msg_id, "raw": fields})
        return result
    except Exception as e:
        return []


def ack(group: str, msg_id: str):
    r.xack(STREAM_KEY, group, msg_id)


def process_with_handler(group: str, consumer: str, handler_fn, max_msgs: int = 50) -> dict:
    ensure_group(group)
    processed = 0
    errors = 0
    msgs = consume(group, consumer, count=max_msgs, block_ms=200)
    for msg in msgs:
        try:
            handler_fn(msg)
            ack(group, msg["id"])
            processed += 1
        except Exception as e:
            errors += 1
            # Send to DLQ
            r.xadd(DLQ_KEY, {"original_id": msg["id"], "error": str(e)[:100], "msg": json.dumps(msg)[:300]}, maxlen=500)
    return {"processed": processed, "errors": errors, "group": group}


def stream_stats() -> dict:
    try:
        info = r.xinfo_stream(STREAM_KEY)
        groups = r.xinfo_groups(STREAM_KEY)
        dlq_len = r.xlen(DLQ_KEY) if r.exists(DLQ_KEY) else 0
        return {
            "stream_len": info.get("length", 0),
            "groups": len(groups),
            "dlq_len": dlq_len,
            "group_names": [g["name"] for g in groups],
        }
    except Exception:
        return {"stream_len": 0, "groups": 0, "dlq_len": 0}


if __name__ == "__main__":
    # Publish some events
    for i in range(5):
        mid = publish(f"test_event_{i}", {"value": i, "source": "test"}, "info")
        print(f"  Published: {mid}")

    # Process with a handler
    received = []
    def handler(msg):
        received.append(msg["type"])

    ensure_group("test_group")
    result = process_with_handler("test_group", "worker_1", handler)
    print(f"  Processed: {result['processed']} msgs, {result['errors']} errors")
    print(f"  Received types: {received[:3]}")
    print(f"  Stream stats: {stream_stats()}")
