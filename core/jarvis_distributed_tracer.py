#!/usr/bin/env python3
"""JARVIS Distributed Tracer — OpenTelemetry-style span tracing for agent calls"""
import redis, json, time, uuid
from datetime import datetime
from contextlib import contextmanager

r = redis.Redis(decode_responses=True)
MAX_TRACES = 1000

class Span:
    def __init__(self, name: str, trace_id: str = None, parent_id: str = None):
        self.span_id = uuid.uuid4().hex[:8]
        self.trace_id = trace_id or uuid.uuid4().hex[:16]
        self.parent_id = parent_id
        self.name = name
        self.start_ms = int(time.time() * 1000)
        self.tags = {}
        self.events = []
    
    def tag(self, key: str, value):
        self.tags[key] = str(value)
        return self
    
    def event(self, name: str, **attrs):
        self.events.append({"name": name, "ts": datetime.now().isoformat()[:19], **attrs})
        return self
    
    def finish(self):
        duration_ms = int(time.time() * 1000) - self.start_ms
        span_data = {
            "span_id": self.span_id, "trace_id": self.trace_id,
            "parent_id": self.parent_id, "name": self.name,
            "duration_ms": duration_ms, "start_ms": self.start_ms,
            "tags": self.tags, "events": self.events,
            "ts": datetime.fromtimestamp(self.start_ms/1000).isoformat()[:19]
        }
        r.lpush("jarvis:traces", json.dumps(span_data))
        r.ltrim("jarvis:traces", 0, MAX_TRACES - 1)
        return span_data

@contextmanager
def trace(name: str, trace_id: str = None, parent_id: str = None, **tags):
    span = Span(name, trace_id, parent_id)
    for k, v in tags.items():
        span.tag(k, v)
    try:
        yield span
        span.event("completed")
    except Exception as e:
        span.event("error", message=str(e)[:100])
        raise
    finally:
        span.finish()

def get_trace(trace_id: str) -> list:
    all_spans = [json.loads(s) for s in r.lrange("jarvis:traces", 0, -1)]
    return [s for s in all_spans if s["trace_id"] == trace_id]

def recent_traces(n: int = 10) -> list:
    return [json.loads(s) for s in r.lrange("jarvis:traces", 0, n-1)]

if __name__ == "__main__":
    # Simulate agent call chain
    with trace("dispatch_request", backend="m2") as root:
        root.tag("model", "qwen3.5-35b")
        tid = root.trace_id
        with trace("llm_router.ask", trace_id=tid, parent_id=root.span_id) as child:
            child.tag("task_type", "code")
            time.sleep(0.05)
            child.event("model_selected", model="qwen3.5-35b")
    
    spans = get_trace(tid)
    print(f"Trace {tid}: {len(spans)} spans")
    for s in spans:
        indent = "  " if s["parent_id"] else ""
        print(f"  {indent}{s['name']}: {s['duration_ms']}ms tags={s['tags']}")
