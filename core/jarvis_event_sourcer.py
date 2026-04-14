#!/usr/bin/env python3
"""
jarvis_event_sourcer — Event sourcing pattern for JARVIS state management
Append-only event log, state reconstruction from events, projections
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.event_sourcer")

EVENTS_FILE = Path("/home/turbo/IA/Core/jarvis/data/events.jsonl")
REDIS_STREAM = "jarvis:event_sourcer:stream"
REDIS_PREFIX = "jarvis:es:"


@dataclass
class Event:
    event_id: str
    event_type: str
    aggregate_id: str
    aggregate_type: str
    payload: dict
    version: int = 1
    ts: float = field(default_factory=time.time)
    causation_id: str = ""  # event that caused this one
    correlation_id: str = ""  # request/session ID

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "aggregate_id": self.aggregate_id,
            "aggregate_type": self.aggregate_type,
            "payload": self.payload,
            "version": self.version,
            "ts": self.ts,
            "causation_id": self.causation_id,
            "correlation_id": self.correlation_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Snapshot:
    aggregate_id: str
    aggregate_type: str
    state: dict
    version: int
    ts: float = field(default_factory=time.time)


class EventSourcer:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._handlers: dict[str, list[Callable]] = {}
        self._projections: dict[str, dict] = {}  # aggregate_id → state
        self._snapshots: dict[str, Snapshot] = {}

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    # ── Write side ─────────────────────────────────────────────────────────────

    async def append(
        self,
        event_type: str,
        aggregate_id: str,
        aggregate_type: str,
        payload: dict,
        causation_id: str = "",
        correlation_id: str = "",
    ) -> Event:
        # Get current version for this aggregate
        current_version = await self._get_version(aggregate_id)
        event = Event(
            event_id=str(uuid.uuid4())[:8],
            event_type=event_type,
            aggregate_id=aggregate_id,
            aggregate_type=aggregate_type,
            payload=payload,
            version=current_version + 1,
            causation_id=causation_id,
            correlation_id=correlation_id,
        )

        # Persist to JSONL
        EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(EVENTS_FILE, "a") as f:
            f.write(json.dumps(event.to_dict()) + "\n")

        if self.redis:
            # Add to Redis Stream
            await self.redis.xadd(
                REDIS_STREAM,
                {
                    "event_id": event.event_id,
                    "event_type": event_type,
                    "aggregate_id": aggregate_id,
                    "aggregate_type": aggregate_type,
                    "payload": json.dumps(payload),
                    "version": str(event.version),
                    "ts": str(event.ts),
                },
                maxlen=100000,
            )
            # Update version counter
            await self.redis.set(f"{REDIS_PREFIX}ver:{aggregate_id}", event.version)

        # Dispatch to handlers
        await self._dispatch(event)

        log.debug(
            f"Event [{event.event_id}] {event_type} agg={aggregate_id} v{event.version}"
        )
        return event

    async def _get_version(self, aggregate_id: str) -> int:
        if self.redis:
            v = await self.redis.get(f"{REDIS_PREFIX}ver:{aggregate_id}")
            if v:
                return int(v)
        return 0

    # ── Handlers / projections ─────────────────────────────────────────────────

    def on(self, event_type: str, handler: Callable):
        """Register a handler for a specific event type."""
        self._handlers.setdefault(event_type, []).append(handler)

    async def _dispatch(self, event: Event):
        handlers = self._handlers.get(event.event_type, []) + self._handlers.get(
            "*", []
        )
        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(event)
                else:
                    handler(event)
            except Exception as e:
                log.error(f"Handler error for {event.event_type}: {e}")

    # ── Read side ──────────────────────────────────────────────────────────────

    def load_events(
        self,
        aggregate_id: str | None = None,
        event_type: str | None = None,
        since: float = 0.0,
        limit: int = 1000,
    ) -> list[Event]:
        if not EVENTS_FILE.exists():
            return []
        events = []
        try:
            for line in EVENTS_FILE.read_text().strip().split("\n"):
                if not line.strip():
                    continue
                d = json.loads(line)
                if aggregate_id and d["aggregate_id"] != aggregate_id:
                    continue
                if event_type and d["event_type"] != event_type:
                    continue
                if d["ts"] < since:
                    continue
                events.append(Event.from_dict(d))
                if len(events) >= limit:
                    break
        except Exception as e:
            log.error(f"Load events error: {e}")
        return events

    def rebuild_state(
        self,
        aggregate_id: str,
        reducer: Callable[[dict, Event], dict],
        initial_state: dict | None = None,
    ) -> dict:
        """Rebuild aggregate state by replaying events."""
        state = initial_state or {}

        # Start from snapshot if available
        snap = self._snapshots.get(aggregate_id)
        since = 0.0
        if snap:
            state = dict(snap.state)
            since = snap.ts

        events = self.load_events(aggregate_id=aggregate_id, since=since)
        for event in events:
            try:
                state = reducer(state, event)
            except Exception as e:
                log.error(f"Reducer error for {event.event_id}: {e}")

        return state

    def take_snapshot(
        self, aggregate_id: str, aggregate_type: str, state: dict, version: int
    ):
        snap = Snapshot(
            aggregate_id=aggregate_id,
            aggregate_type=aggregate_type,
            state=state,
            version=version,
        )
        self._snapshots[aggregate_id] = snap
        # Persist snapshot
        snap_file = EVENTS_FILE.parent / f"snapshot_{aggregate_id}.json"
        snap_file.write_text(
            json.dumps(
                {
                    "aggregate_id": snap.aggregate_id,
                    "aggregate_type": snap.aggregate_type,
                    "state": snap.state,
                    "version": snap.version,
                    "ts": snap.ts,
                }
            )
        )

    def stats(self) -> dict:
        if not EVENTS_FILE.exists():
            return {"total_events": 0}
        events = self.load_events(limit=100000)
        by_type: dict[str, int] = {}
        by_agg: dict[str, int] = {}
        for e in events:
            by_type[e.event_type] = by_type.get(e.event_type, 0) + 1
            by_agg[e.aggregate_type] = by_agg.get(e.aggregate_type, 0) + 1
        return {
            "total_events": len(events),
            "event_types": sorted(by_type.items(), key=lambda x: -x[1])[:10],
            "aggregate_types": sorted(by_agg.items(), key=lambda x: -x[1])[:10],
        }


async def main():
    import sys

    es = EventSourcer()
    await es.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        # Model lifecycle events
        await es.append(
            "model_loaded", "qwen3.5-9b", "model", {"backend": "M1", "vram_mb": 6400}
        )
        await es.append(
            "model_requested", "qwen3.5-9b", "model", {"tokens": 512, "user": "turbo"}
        )
        await es.append(
            "model_responded",
            "qwen3.5-9b",
            "model",
            {"latency_ms": 243, "tokens_out": 180},
        )
        await es.append("model_unloaded", "qwen3.5-9b", "model", {"reason": "idle"})

        # Reconstruct model state
        def model_reducer(state: dict, event: Event) -> dict:
            if event.event_type == "model_loaded":
                state.update({"status": "loaded", **event.payload})
            elif event.event_type == "model_requested":
                state["requests"] = state.get("requests", 0) + 1
            elif event.event_type == "model_unloaded":
                state["status"] = "unloaded"
            return state

        state = es.rebuild_state("qwen3.5-9b", model_reducer)
        print(f"Rebuilt state: {state}")

        s = es.stats()
        print(f"\nStats: {s['total_events']} events")
        for etype, count in s["event_types"]:
            print(f"  {etype}: {count}")

    elif cmd == "stats":
        s = es.stats()
        print(json.dumps(s, indent=2))

    elif cmd == "log" and len(sys.argv) > 2:
        events = es.load_events(aggregate_id=sys.argv[2])
        for e in events:
            ts = time.strftime("%H:%M:%S", time.localtime(e.ts))
            print(f"  [{ts}] v{e.version} {e.event_type} {e.payload}")


if __name__ == "__main__":
    asyncio.run(main())
