#!/usr/bin/env python3
"""
jarvis_event_aggregator — Time-window event aggregation and pattern detection
Groups, counts, and analyzes events within tumbling/sliding windows
"""

import asyncio
import json
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Callable

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.event_aggregator")

REDIS_PREFIX = "jarvis:evtagg:"


@dataclass
class Event:
    event_type: str
    source: str
    data: dict
    ts: float = field(default_factory=time.time)
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "event_type": self.event_type,
            "source": self.source,
            "data": self.data,
            "ts": self.ts,
            "tags": self.tags,
        }


@dataclass
class AggregationWindow:
    window_id: str
    event_type: str
    window_s: float
    start: float
    end: float
    count: int = 0
    events: list[Event] = field(default_factory=list)
    sources: set = field(default_factory=set)

    @property
    def rate_per_s(self) -> float:
        duration = max(self.end - self.start, 1.0)
        return round(self.count / duration, 2)

    def to_dict(self) -> dict:
        return {
            "window_id": self.window_id,
            "event_type": self.event_type,
            "window_s": self.window_s,
            "start": self.start,
            "end": self.end,
            "count": self.count,
            "rate_per_s": self.rate_per_s,
            "sources": list(self.sources),
        }


@dataclass
class AggregationRule:
    name: str
    event_type: str  # event type pattern (or "*" for all)
    window_s: float  # window size in seconds
    threshold: int = 0  # fire alert if count exceeds this
    callback: Callable | None = None

    def matches(self, event: Event) -> bool:
        return self.event_type == "*" or event.event_type == self.event_type


class EventAggregator:
    def __init__(self, max_buffer: int = 10000):
        self.redis: aioredis.Redis | None = None
        self._buffer: deque[Event] = deque(maxlen=max_buffer)
        self._rules: list[AggregationRule] = []
        self._windows: dict[str, list[AggregationWindow]] = defaultdict(list)
        self._callbacks: list[Callable] = []
        self._stats: dict[str, int] = defaultdict(int)

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def add_rule(self, rule: AggregationRule):
        self._rules.append(rule)

    def on_alert(self, callback: Callable):
        """Register callback for threshold alerts: callback(rule, window)"""
        self._callbacks.append(callback)

    def ingest(
        self, event_type: str, source: str, data: dict, tags: list[str] | None = None
    ) -> Event:
        event = Event(event_type=event_type, source=source, data=data, tags=tags or [])
        self._buffer.append(event)
        self._stats[event_type] += 1

        # Process matching rules
        for rule in self._rules:
            if rule.matches(event):
                self._process_rule(rule, event)

        if self.redis:
            asyncio.create_task(self._publish(event))

        return event

    def _process_rule(self, rule: AggregationRule, event: Event):
        now = event.ts
        window_start = now - rule.window_s

        # Get or create current window
        windows = self._windows[rule.name]

        # Find active window (created within the last window_s)
        active = None
        for w in windows:
            if w.start >= window_start:
                active = w
                break

        if not active:
            active = AggregationWindow(
                window_id=f"{rule.name}:{int(now)}",
                event_type=rule.event_type,
                window_s=rule.window_s,
                start=now,
                end=now,
            )
            windows.append(active)
            # Keep only last 100 windows per rule
            if len(windows) > 100:
                windows.pop(0)

        active.count += 1
        active.end = now
        active.sources.add(event.source)
        active.events.append(event)

        # Threshold alert
        if rule.threshold and active.count >= rule.threshold:
            log.warning(
                f"Alert: rule={rule.name} count={active.count} in {rule.window_s}s window"
            )
            for cb in self._callbacks:
                try:
                    cb(rule, active)
                except Exception as e:
                    log.debug(f"Alert callback error: {e}")
            if rule.callback:
                try:
                    rule.callback(rule, active)
                except Exception as e:
                    log.debug(f"Rule callback error: {e}")

    async def _publish(self, event: Event):
        if self.redis:
            await self.redis.publish(
                "jarvis:events",
                json.dumps({"source": "event_aggregator", **event.to_dict()}),
            )

    def tumbling_window(self, event_type: str, window_s: float) -> AggregationWindow:
        """Get aggregation for a fixed tumbling window ending now."""
        now = time.time()
        start = now - window_s
        matching = [
            e for e in self._buffer if e.event_type == event_type and e.ts >= start
        ]
        return AggregationWindow(
            window_id=f"tumbling:{event_type}:{int(window_s)}",
            event_type=event_type,
            window_s=window_s,
            start=start,
            end=now,
            count=len(matching),
            events=matching,
            sources={e.source for e in matching},
        )

    def sliding_windows(
        self, event_type: str, window_s: float, step_s: float, periods: int = 5
    ) -> list[AggregationWindow]:
        """Get multiple overlapping windows (sliding)."""
        now = time.time()
        result = []
        for i in range(periods):
            end = now - i * step_s
            start = end - window_s
            matching = [
                e
                for e in self._buffer
                if e.event_type == event_type and start <= e.ts <= end
            ]
            result.append(
                AggregationWindow(
                    window_id=f"sliding:{event_type}:{i}",
                    event_type=event_type,
                    window_s=window_s,
                    start=start,
                    end=end,
                    count=len(matching),
                    sources={e.source for e in matching},
                )
            )
        return result

    def top_sources(
        self, event_type: str, window_s: float = 60.0, top_n: int = 5
    ) -> list[dict]:
        cutoff = time.time() - window_s
        matching = [
            e for e in self._buffer if e.event_type == event_type and e.ts >= cutoff
        ]
        counts: dict[str, int] = defaultdict(int)
        for e in matching:
            counts[e.source] += 1
        return [
            {"source": src, "count": cnt}
            for src, cnt in sorted(counts.items(), key=lambda x: x[1], reverse=True)[
                :top_n
            ]
        ]

    def event_rate(self, event_type: str, window_s: float = 60.0) -> float:
        w = self.tumbling_window(event_type, window_s)
        return w.rate_per_s

    def recent(self, event_type: str | None = None, limit: int = 20) -> list[dict]:
        events = list(self._buffer)
        if event_type:
            events = [e for e in events if e.event_type == event_type]
        return [e.to_dict() for e in reversed(events[-limit:])]

    def stats(self) -> dict:
        return {
            "buffer_size": len(self._buffer),
            "rules": len(self._rules),
            "event_types": dict(self._stats),
            "total_events": sum(self._stats.values()),
        }


async def main():
    import sys

    agg = EventAggregator()
    await agg.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        # Set up rules
        agg.add_rule(
            AggregationRule(
                name="error_burst",
                event_type="error",
                window_s=10.0,
                threshold=3,
                callback=lambda r, w: print(
                    f"  ALERT: {r.name} — {w.count} errors in {r.window_s}s"
                ),
            )
        )
        agg.add_rule(
            AggregationRule(name="all_requests", event_type="*", window_s=60.0)
        )

        # Ingest events
        for i in range(5):
            agg.ingest("request", "M1", {"path": "/infer", "latency_ms": 240 + i * 10})
            agg.ingest("request", "M2", {"path": "/infer", "latency_ms": 180 + i * 5})
        for i in range(4):
            agg.ingest("error", "M1", {"code": 500, "msg": f"Internal error #{i}"})

        # Query windows
        w = agg.tumbling_window("request", 60.0)
        print(
            f"\nRequests (60s window): count={w.count} rate={w.rate_per_s}/s sources={w.sources}"
        )

        top = agg.top_sources("request", window_s=60.0)
        print(f"Top sources: {top}")

        print(f"\nStats: {json.dumps(agg.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(agg.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
