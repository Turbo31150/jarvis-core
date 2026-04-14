#!/usr/bin/env python3
"""JARVIS Trace Collector — Collect distributed traces for request debugging"""

import redis
import json
import uuid
import time
from datetime import datetime

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:traces"
TRACE_TTL = 3600


class Span:
    def __init__(self, trace_id: str, name: str, parent_id: str = None):
        self.trace_id = trace_id
        self.span_id = uuid.uuid4().hex[:8]
        self.name = name
        self.parent_id = parent_id
        self.start_time = time.perf_counter()
        self.start_ts = datetime.now().isoformat()[:23]
        self.tags = {}
        self.events = []
        self.status = "ok"

    def tag(self, key: str, value):
        self.tags[key] = str(value)
        return self

    def event(self, message: str):
        self.events.append({"ts": datetime.now().isoformat()[:23], "msg": message})
        return self

    def error(self, message: str):
        self.status = "error"
        self.events.append({"ts": datetime.now().isoformat()[:23], "msg": f"ERROR: {message}"})
        return self

    def finish(self) -> dict:
        duration_ms = round((time.perf_counter() - self.start_time) * 1000)
        span_data = {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "name": self.name,
            "start_ts": self.start_ts,
            "duration_ms": duration_ms,
            "status": self.status,
            "tags": self.tags,
            "events": self.events,
        }
        # Store in Redis
        r.lpush(f"{PREFIX}:{self.trace_id}", json.dumps(span_data))
        r.expire(f"{PREFIX}:{self.trace_id}", TRACE_TTL)
        r.lpush(f"{PREFIX}:recent", json.dumps(span_data))
        r.ltrim(f"{PREFIX}:recent", 0, 999)
        return span_data


def start_trace(name: str = "request") -> tuple[str, Span]:
    trace_id = uuid.uuid4().hex[:12]
    span = Span(trace_id, name)
    return trace_id, span


def get_trace(trace_id: str) -> list:
    raw = r.lrange(f"{PREFIX}:{trace_id}", 0, -1)
    return [json.loads(s) for s in raw]


def recent_traces(n: int = 20) -> list:
    raw = r.lrange(f"{PREFIX}:recent", 0, n - 1)
    return [json.loads(s) for s in raw]


def stats() -> dict:
    recent = recent_traces(100)
    errors = sum(1 for s in recent if s.get("status") == "error")
    avg_ms = sum(s.get("duration_ms", 0) for s in recent) // max(len(recent), 1)
    return {
        "recent_spans": len(recent),
        "error_rate_pct": round(errors / max(len(recent), 1) * 100, 1),
        "avg_duration_ms": avg_ms,
    }


if __name__ == "__main__":
    # Simulate a request trace
    tid, root = start_trace("api_request")
    root.tag("endpoint", "/llm/ask").tag("method", "POST")
    time.sleep(0.01)

    # Child span: LLM routing
    route_span = Span(tid, "llm_routing", parent_id=root.span_id)
    route_span.tag("backend", "ol1_gemma3").tag("task_type", "fast")
    time.sleep(0.005)
    route_span.event("Selected ol1 backend").finish()

    # Child span: LLM call
    llm_span = Span(tid, "llm_call", parent_id=root.span_id)
    llm_span.tag("model", "gemma3:4b")
    time.sleep(0.02)
    llm_span.event("Response received").finish()

    root_data = root.finish()
    print(f"Trace {tid}: {root_data['duration_ms']}ms")
    spans = get_trace(tid)
    for s in spans:
        indent = "  " if s.get("parent_id") else ""
        print(f"  {indent}[{s['name']}] {s['duration_ms']}ms {s['status']}")
    print(f"Stats: {stats()}")
