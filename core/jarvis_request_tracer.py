#!/usr/bin/env python3
"""JARVIS Request Tracer — Distributed tracing for LLM request chains with span tracking"""

import redis
import json
import time
import uuid

r = redis.Redis(decode_responses=True)

TRACE_PREFIX = "jarvis:trace:"
SPAN_PREFIX = "jarvis:trace:span:"
INDEX_KEY = "jarvis:trace:index"
STATS_KEY = "jarvis:trace:stats"


def new_trace(name: str, identity: str = "anon", metadata: dict = None) -> str:
    tid = uuid.uuid4().hex[:16]
    trace = {
        "trace_id": tid,
        "name": name,
        "identity": identity,
        "metadata": metadata or {},
        "started_at": time.time(),
        "spans": [],
        "status": "active",
    }
    r.setex(f"{TRACE_PREFIX}{tid}", 3600, json.dumps(trace))
    r.zadd(INDEX_KEY, {tid: time.time()})
    r.expire(INDEX_KEY, 86400)
    r.hincrby(STATS_KEY, "traces_created", 1)
    return tid


def start_span(
    trace_id: str, name: str, service: str = "", parent_span: str = None
) -> str:
    sid = uuid.uuid4().hex[:12]
    span = {
        "span_id": sid,
        "trace_id": trace_id,
        "name": name,
        "service": service,
        "parent": parent_span,
        "started_at": time.time(),
        "finished_at": None,
        "duration_ms": None,
        "status": "active",
        "tags": {},
        "logs": [],
    }
    r.setex(f"{SPAN_PREFIX}{sid}", 3600, json.dumps(span))
    return sid


def finish_span(
    span_id: str, success: bool = True, tags: dict = None, error: str = None
):
    raw = r.get(f"{SPAN_PREFIX}{span_id}")
    if not raw:
        return
    span = json.loads(raw)
    now = time.time()
    span["finished_at"] = now
    span["duration_ms"] = round((now - span["started_at"]) * 1000)
    span["status"] = "ok" if success else "error"
    if tags:
        span["tags"].update(tags)
    if error:
        span["error"] = error[:200]

    r.setex(f"{SPAN_PREFIX}{span_id}", 3600, json.dumps(span))

    # Attach span to trace
    trace_raw = r.get(f"{TRACE_PREFIX}{span['trace_id']}")
    if trace_raw:
        trace = json.loads(trace_raw)
        trace["spans"].append(span_id)
        r.setex(f"{TRACE_PREFIX}{span['trace_id']}", 3600, json.dumps(trace))

    r.hincrby(STATS_KEY, "spans_finished", 1)


def log_span(span_id: str, message: str, level: str = "info"):
    raw = r.get(f"{SPAN_PREFIX}{span_id}")
    if not raw:
        return
    span = json.loads(raw)
    span["logs"].append({"msg": message[:200], "level": level, "ts": time.time()})
    r.setex(f"{SPAN_PREFIX}{span_id}", 3600, json.dumps(span))


def finish_trace(trace_id: str, status: str = "ok"):
    raw = r.get(f"{TRACE_PREFIX}{trace_id}")
    if not raw:
        return
    trace = json.loads(raw)
    trace["finished_at"] = time.time()
    trace["duration_ms"] = round((trace["finished_at"] - trace["started_at"]) * 1000)
    trace["status"] = status
    r.setex(f"{TRACE_PREFIX}{trace_id}", 3600, json.dumps(trace))
    r.hincrby(STATS_KEY, "traces_finished", 1)


def get_trace(trace_id: str) -> dict | None:
    raw = r.get(f"{TRACE_PREFIX}{trace_id}")
    if not raw:
        return None
    trace = json.loads(raw)
    spans = []
    for sid in trace.get("spans", []):
        span_raw = r.get(f"{SPAN_PREFIX}{sid}")
        if span_raw:
            spans.append(json.loads(span_raw))
    trace["span_details"] = sorted(spans, key=lambda x: x["started_at"])
    return trace


def recent_traces(limit: int = 10) -> list:
    tids = r.zrevrange(INDEX_KEY, 0, limit - 1)
    traces = []
    for tid in tids:
        raw = r.get(f"{TRACE_PREFIX}{tid}")
        if raw:
            traces.append(json.loads(raw))
    return traces


def stats() -> dict:
    s = r.hgetall(STATS_KEY)
    return {k: int(v) for k, v in s.items()}


if __name__ == "__main__":

    # Simulate a full RAG pipeline trace
    tid = new_trace(
        "rag_query",
        identity="turbo",
        metadata={"query": "Quel est l'état du cluster ?"},
    )

    s1 = start_span(tid, "intent_classify", service="intent_router")
    time.sleep(0.01)
    finish_span(s1, tags={"intent": "cluster_health", "confidence": 0.92})

    s2 = start_span(tid, "kb_search", service="knowledge_base", parent_span=s1)
    time.sleep(0.02)
    log_span(s2, "Found 3 matching documents", level="info")
    finish_span(s2, tags={"results": 3, "top_score": 0.87})

    s3 = start_span(tid, "embed_rerank", service="reranker", parent_span=s2)
    time.sleep(0.015)
    finish_span(s3, tags={"reranked": 3})

    s4 = start_span(tid, "llm_generate", service="m32_mistral", parent_span=s1)
    time.sleep(0.05)
    log_span(s4, "Streaming response started", level="info")
    finish_span(s4, tags={"model": "mistral-7b", "tokens": 120, "latency_ms": 142})

    finish_trace(tid)

    trace = get_trace(tid)
    print(f"Trace: {trace['name']} [{trace['status']}] {trace.get('duration_ms', 0)}ms")
    for span in trace["span_details"]:
        indent = "  " if span.get("parent") else ""
        icon = "✅" if span["status"] == "ok" else "❌"
        print(
            f"  {indent}{icon} [{span['service']:15s}] {span['name']:20s} "
            f"{span['duration_ms']}ms  {span.get('tags', {})}"
        )

    print(f"\nStats: {stats()}")
