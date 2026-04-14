#!/usr/bin/env python3
"""
jarvis_request_tracer_v2 — Distributed request tracing with span hierarchy
OpenTelemetry-compatible trace/span model for end-to-end request visibility
"""

import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.request_tracer_v2")

REDIS_PREFIX = "jarvis:trace2:"
TRACE_LOG = Path("/home/turbo/IA/Core/jarvis/data/traces.jsonl")


@dataclass
class Span:
    span_id: str
    trace_id: str
    parent_span_id: str
    operation: str
    service: str
    started_at: float
    ended_at: float = 0.0
    status: str = "ok"  # ok | error | timeout
    tags: dict = field(default_factory=dict)
    logs: list[dict] = field(default_factory=list)
    error: str = ""

    @property
    def duration_ms(self) -> float:
        end = self.ended_at or time.time()
        return round((end - self.started_at) * 1000, 1)

    @property
    def active(self) -> bool:
        return self.ended_at == 0.0

    def log(self, message: str, level: str = "info", **fields):
        self.logs.append(
            {"ts": time.time(), "level": level, "message": message, **fields}
        )

    def set_tag(self, key: str, value: Any):
        self.tags[key] = value

    def finish(self, status: str = "ok", error: str = ""):
        self.ended_at = time.time()
        self.status = status
        if error:
            self.error = error

    def to_dict(self) -> dict:
        return {
            "span_id": self.span_id,
            "trace_id": self.trace_id,
            "parent_span_id": self.parent_span_id,
            "operation": self.operation,
            "service": self.service,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "tags": self.tags,
            "error": self.error,
            "log_count": len(self.logs),
        }


@dataclass
class Trace:
    trace_id: str
    root_span: Span
    spans: list[Span] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    ended_at: float = 0.0
    service: str = "jarvis"

    @property
    def duration_ms(self) -> float:
        end = self.ended_at or time.time()
        return round((end - self.started_at) * 1000, 1)

    @property
    def all_spans(self) -> list[Span]:
        return [self.root_span] + self.spans

    @property
    def error_spans(self) -> list[Span]:
        return [s for s in self.all_spans if s.status == "error"]

    @property
    def critical_path_ms(self) -> float:
        """Longest sequential chain duration."""
        return max((s.duration_ms for s in self.all_spans), default=0.0)

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "service": self.service,
            "duration_ms": self.duration_ms,
            "span_count": len(self.all_spans),
            "error_count": len(self.error_spans),
            "critical_path_ms": self.critical_path_ms,
            "started_at": self.started_at,
            "spans": [s.to_dict() for s in self.all_spans],
        }


class RequestTracerV2:
    def __init__(self, service: str = "jarvis"):
        self.redis: aioredis.Redis | None = None
        self.service = service
        self._traces: dict[str, Trace] = {}
        self._active_span: dict[str, str] = {}  # trace_id → current span_id
        TRACE_LOG.parent.mkdir(parents=True, exist_ok=True)

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def start_trace(
        self,
        operation: str,
        service: str | None = None,
        tags: dict | None = None,
        trace_id: str | None = None,
    ) -> Trace:
        tid = trace_id or str(uuid.uuid4()).replace("-", "")[:16]
        root = Span(
            span_id=str(uuid.uuid4())[:8],
            trace_id=tid,
            parent_span_id="",
            operation=operation,
            service=service or self.service,
            started_at=time.time(),
            tags=tags or {},
        )
        trace = Trace(trace_id=tid, root_span=root, service=service or self.service)
        self._traces[tid] = trace
        self._active_span[tid] = root.span_id
        log.debug(f"Trace started: [{tid}] {operation}")
        return trace

    def start_span(
        self,
        trace_id: str,
        operation: str,
        service: str | None = None,
        parent_span_id: str | None = None,
        tags: dict | None = None,
    ) -> Span:
        trace = self._traces.get(trace_id)
        if not trace:
            raise ValueError(f"Trace '{trace_id}' not found")

        parent = parent_span_id or self._active_span.get(
            trace_id, trace.root_span.span_id
        )
        span = Span(
            span_id=str(uuid.uuid4())[:8],
            trace_id=trace_id,
            parent_span_id=parent,
            operation=operation,
            service=service or self.service,
            started_at=time.time(),
            tags=tags or {},
        )
        trace.spans.append(span)
        self._active_span[trace_id] = span.span_id
        return span

    @asynccontextmanager
    async def span(
        self,
        trace_id: str,
        operation: str,
        service: str | None = None,
        tags: dict | None = None,
    ) -> AsyncIterator[Span]:
        s = self.start_span(trace_id, operation, service=service, tags=tags)
        try:
            yield s
            if s.active:
                s.finish("ok")
        except Exception as e:
            s.finish("error", str(e)[:200])
            raise
        finally:
            # Restore parent as active
            parent_id = s.parent_span_id
            if parent_id:
                self._active_span[trace_id] = parent_id

    def finish_trace(self, trace_id: str, status: str = "ok"):
        trace = self._traces.get(trace_id)
        if not trace:
            return
        trace.ended_at = time.time()
        trace.root_span.finish(status)

        # Finish any still-active spans
        for span in trace.spans:
            if span.active:
                span.finish("ok")

        self._active_span.pop(trace_id, None)
        self._persist(trace)
        log.debug(
            f"Trace finished: [{trace_id}] {trace.duration_ms:.0f}ms {len(trace.error_spans)} errors"
        )

        if self.redis:
            asyncio.create_task(self._push_redis(trace))

    def _persist(self, trace: Trace):
        with open(TRACE_LOG, "a") as f:
            summary = {
                "trace_id": trace.trace_id,
                "service": trace.service,
                "duration_ms": trace.duration_ms,
                "spans": len(trace.all_spans),
                "errors": len(trace.error_spans),
                "ts": trace.started_at,
            }
            f.write(json.dumps(summary) + "\n")

    async def _push_redis(self, trace: Trace):
        await self.redis.setex(
            f"{REDIS_PREFIX}{trace.trace_id}",
            3600,
            json.dumps(trace.to_dict()),
        )
        await self.redis.lpush(f"{REDIS_PREFIX}recent", trace.trace_id)
        await self.redis.ltrim(f"{REDIS_PREFIX}recent", 0, 999)

    def get_trace(self, trace_id: str) -> Trace | None:
        return self._traces.get(trace_id)

    def waterfall(self, trace_id: str) -> list[dict]:
        """Return spans sorted by start time for waterfall view."""
        trace = self._traces.get(trace_id)
        if not trace:
            return []
        spans = sorted(trace.all_spans, key=lambda s: s.started_at)
        base = spans[0].started_at if spans else time.time()
        return [
            {
                "operation": s.operation,
                "service": s.service,
                "offset_ms": round((s.started_at - base) * 1000, 1),
                "duration_ms": s.duration_ms,
                "status": s.status,
                "depth": 0 if not s.parent_span_id else 1,
            }
            for s in spans
        ]

    def stats(self) -> dict:
        traces = list(self._traces.values())
        completed = [t for t in traces if t.ended_at]
        return {
            "total_traces": len(traces),
            "completed": len(completed),
            "active": len(traces) - len(completed),
            "avg_duration_ms": round(
                sum(t.duration_ms for t in completed) / max(len(completed), 1), 1
            ),
            "error_traces": sum(1 for t in completed if t.error_spans),
            "saved": len(list(TRACE_LOG.parent.glob("traces.jsonl"))),
        }


async def main():
    import sys

    tracer = RequestTracerV2(service="jarvis-api")
    await tracer.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        trace = tracer.start_trace(
            "handle_request", tags={"user": "turbo", "path": "/infer"}
        )
        tid = trace.trace_id

        async with tracer.span(tid, "validate_input") as s:
            s.set_tag("payload_size", 512)
            await asyncio.sleep(0.005)

        async with tracer.span(tid, "route_model") as s:
            s.set_tag("selected_model", "qwen3.5-9b")
            await asyncio.sleep(0.002)

        async with tracer.span(tid, "llm_inference") as s:
            s.set_tag("model", "qwen3.5-9b")
            s.set_tag("node", "M1")
            s.log("Starting LLM call")
            await asyncio.sleep(0.25)
            s.set_tag("tokens_out", 128)

        async with tracer.span(tid, "format_response") as s:
            await asyncio.sleep(0.001)

        tracer.finish_trace(tid)

        print(f"Trace [{tid}]: {trace.duration_ms:.0f}ms, {len(trace.all_spans)} spans")
        print("\nWaterfall:")
        for entry in tracer.waterfall(tid):
            indent = "  " * entry["depth"]
            print(
                f"  {indent}{entry['operation']:<25} +{entry['offset_ms']:>6.1f}ms  [{entry['duration_ms']:>6.1f}ms] {entry['status']}"
            )

        print(f"\nStats: {json.dumps(tracer.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(tracer.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
