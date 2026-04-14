#!/usr/bin/env python3
"""
jarvis_event_log — Structured event logging with filtering, replay, and Redis pub/sub
Immutable append-only log for system events, agent actions, and audit trails
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.event_log")

REDIS_PREFIX = "jarvis:eventlog:"
REDIS_CHANNEL = "jarvis:events"
LOG_FILE = Path("/home/turbo/IA/Core/jarvis/data/event_log.jsonl")


class EventLevel(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class EventKind(str, Enum):
    SYSTEM = "system"
    AGENT = "agent"
    MODEL = "model"
    CLUSTER = "cluster"
    SECURITY = "security"
    TRADING = "trading"
    USER = "user"
    AUDIT = "audit"


@dataclass
class Event:
    event_id: str
    kind: EventKind
    level: EventLevel
    source: str  # component/agent that emitted the event
    action: str  # verb: started, failed, completed, detected, ...
    message: str
    payload: dict = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    ts: float = field(default_factory=time.time)
    correlation_id: str = ""  # link related events across components

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "kind": self.kind.value,
            "level": self.level.value,
            "source": self.source,
            "action": self.action,
            "message": self.message,
            "payload": self.payload,
            "tags": self.tags,
            "ts": self.ts,
            "correlation_id": self.correlation_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        return cls(
            event_id=d["event_id"],
            kind=EventKind(d.get("kind", "system")),
            level=EventLevel(d.get("level", "info")),
            source=d.get("source", ""),
            action=d.get("action", ""),
            message=d.get("message", ""),
            payload=d.get("payload", {}),
            tags=d.get("tags", []),
            ts=d.get("ts", time.time()),
            correlation_id=d.get("correlation_id", ""),
        )


@dataclass
class EventFilter:
    kind: EventKind | None = None
    level: EventLevel | None = None
    source: str | None = None
    action: str | None = None
    tags: list[str] | None = None
    since_ts: float = 0.0
    until_ts: float = 0.0
    correlation_id: str = ""
    limit: int = 100

    def matches(self, event: Event) -> bool:
        if self.kind and event.kind != self.kind:
            return False
        if self.level and event.level != self.level:
            return False
        if self.source and self.source not in event.source:
            return False
        if self.action and self.action not in event.action:
            return False
        if self.tags and not any(t in event.tags for t in self.tags):
            return False
        if self.since_ts and event.ts < self.since_ts:
            return False
        if self.until_ts and event.ts > self.until_ts:
            return False
        if self.correlation_id and event.correlation_id != self.correlation_id:
            return False
        return True


class EventLog:
    def __init__(self, persist: bool = True, max_memory: int = 10000):
        self.redis: aioredis.Redis | None = None
        self._events: list[Event] = []
        self._persist = persist
        self._max_memory = max_memory
        self._subscribers: list[asyncio.Queue] = []
        self._stats: dict[str, int] = {
            "total": 0,
            "by_level_error": 0,
            "by_level_warning": 0,
            "by_level_critical": 0,
            "published": 0,
        }
        if persist:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            self._load()

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def _load(self):
        if not LOG_FILE.exists():
            return
        try:
            lines = LOG_FILE.read_text().splitlines()
            # Load last max_memory events only
            for line in lines[-self._max_memory :]:
                if line.strip():
                    d = json.loads(line)
                    self._events.append(Event.from_dict(d))
        except Exception as e:
            log.warning(f"Event log load error: {e}")

    def emit(
        self,
        kind: EventKind,
        level: EventLevel,
        source: str,
        action: str,
        message: str,
        payload: dict | None = None,
        tags: list[str] | None = None,
        correlation_id: str = "",
    ) -> Event:
        event = Event(
            event_id=str(uuid.uuid4())[:12],
            kind=kind,
            level=level,
            source=source,
            action=action,
            message=message,
            payload=payload or {},
            tags=tags or [],
            correlation_id=correlation_id,
        )
        self._events.append(event)
        if len(self._events) > self._max_memory:
            self._events.pop(0)

        self._stats["total"] += 1
        if level == EventLevel.ERROR:
            self._stats["by_level_error"] += 1
        elif level == EventLevel.WARNING:
            self._stats["by_level_warning"] += 1
        elif level == EventLevel.CRITICAL:
            self._stats["by_level_critical"] += 1

        if self._persist:
            try:
                with open(LOG_FILE, "a") as f:
                    f.write(json.dumps(event.to_dict()) + "\n")
            except Exception:
                pass

        # Fan-out to subscribers
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

        # Publish to Redis
        if self.redis:
            self._stats["published"] += 1
            asyncio.create_task(
                self.redis.publish(REDIS_CHANNEL, json.dumps(event.to_dict()))
            )

        return event

    # Convenience emitters
    def info(self, source: str, action: str, message: str, **kwargs) -> Event:
        return self.emit(
            EventKind.SYSTEM, EventLevel.INFO, source, action, message, **kwargs
        )

    def warning(self, source: str, action: str, message: str, **kwargs) -> Event:
        return self.emit(
            EventKind.SYSTEM, EventLevel.WARNING, source, action, message, **kwargs
        )

    def error(self, source: str, action: str, message: str, **kwargs) -> Event:
        return self.emit(
            EventKind.SYSTEM, EventLevel.ERROR, source, action, message, **kwargs
        )

    def audit(self, source: str, action: str, message: str, **kwargs) -> Event:
        return self.emit(
            EventKind.AUDIT, EventLevel.INFO, source, action, message, **kwargs
        )

    def subscribe(self, maxsize: int = 100) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        if q in self._subscribers:
            self._subscribers.remove(q)

    def query(self, f: EventFilter) -> list[Event]:
        results = [e for e in reversed(self._events) if f.matches(e)]
        return results[: f.limit]

    def tail(self, n: int = 20, level: EventLevel | None = None) -> list[dict]:
        events = self._events
        if level:
            events = [e for e in events if e.level == level]
        return [e.to_dict() for e in events[-n:]]

    def replay(self, since_ts: float, kind: EventKind | None = None) -> list[Event]:
        f = EventFilter(since_ts=since_ts, kind=kind, limit=10000)
        return list(reversed(self.query(f)))

    def stats(self) -> dict:
        return {**self._stats, "memory_size": len(self._events)}


_global_log: EventLog | None = None


def get_event_log() -> EventLog:
    global _global_log
    if _global_log is None:
        _global_log = EventLog()
    return _global_log


def build_jarvis_event_log() -> EventLog:
    return EventLog(persist=True, max_memory=10000)


async def main():
    import sys

    elog = build_jarvis_event_log()
    await elog.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        # Subscribe before emitting
        q = elog.subscribe()

        events = [
            (
                EventKind.SYSTEM,
                EventLevel.INFO,
                "jarvis_core",
                "started",
                "JARVIS system started",
            ),
            (
                EventKind.CLUSTER,
                EventLevel.WARNING,
                "node_m2",
                "degraded",
                "M2 latency high: 4500ms",
            ),
            (
                EventKind.MODEL,
                EventLevel.INFO,
                "inference_gw",
                "routed",
                "Request routed to qwen3.5-9b",
            ),
            (
                EventKind.SECURITY,
                EventLevel.ERROR,
                "auth_guard",
                "blocked",
                "Invalid API key from 192.168.1.99",
            ),
            (
                EventKind.AUDIT,
                EventLevel.INFO,
                "admin",
                "config_changed",
                "feature_flag.rag_retrieval set to ENABLED",
            ),
        ]
        for kind, level, source, action, message in events:
            elog.emit(kind, level, source, action, message)

        print("Last 5 events:")
        for e in elog.tail(5):
            icon = {"info": "ℹ️", "warning": "⚠️", "error": "❌", "critical": "🔥"}.get(
                e["level"], "·"
            )
            print(
                f"  {icon} [{e['kind']:<10}] {e['source']:<20} {e['action']:<20} {e['message'][:50]}"
            )

        print(f"\nQueue depth: {q.qsize()} events ready")

        f = EventFilter(level=EventLevel.WARNING)
        warnings = elog.query(f)
        print(f"Warnings: {len(warnings)}")

        print(f"\nStats: {json.dumps(elog.stats(), indent=2)}")

    elif cmd == "tail":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 20
        for e in elog.tail(n):
            print(json.dumps(e))

    elif cmd == "stats":
        print(json.dumps(elog.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
