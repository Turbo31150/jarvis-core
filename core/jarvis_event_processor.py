#!/usr/bin/env python3
"""
jarvis_event_processor — Redis pub/sub event processing pipeline
Consumes jarvis:events channel, routes to handlers, supports filters/transforms
"""

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.event_processor")

EVENTS_CHANNEL = "jarvis:events"
DEAD_LETTER_KEY = "jarvis:events:dlq"
STATS_KEY = "jarvis:events:stats"


@dataclass
class JarvisEvent:
    event: str
    data: dict
    received_at: float = field(default_factory=time.time)
    source_channel: str = EVENTS_CHANNEL

    @classmethod
    def from_message(
        cls, raw: str, channel: str = EVENTS_CHANNEL
    ) -> "JarvisEvent | None":
        try:
            d = json.loads(raw)
            return cls(
                event=d.get("event", "unknown"),
                data=d,
                source_channel=channel,
            )
        except Exception:
            return None

    def matches(self, pattern: str) -> bool:
        """Glob-style pattern match on event name."""
        if pattern == "*":
            return True
        if pattern.endswith("*"):
            return self.event.startswith(pattern[:-1])
        return self.event == pattern


@dataclass
class EventHandler:
    name: str
    pattern: str
    handler: Callable
    priority: int = 5
    enabled: bool = True
    call_count: int = 0
    error_count: int = 0
    last_called: float = 0.0


class EventProcessor:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._handlers: list[EventHandler] = []
        self._running = False
        self._stats: dict[str, int] = {
            "received": 0,
            "processed": 0,
            "errors": 0,
            "dlq": 0,
        }
        self._register_defaults()

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception as e:
            log.error(f"Redis connect failed: {e}")
            self.redis = None

    def _register_defaults(self):
        """Register built-in event handlers."""
        self.on("thermal_action", self._handle_thermal, priority=9)
        self.on("budget_pressure", self._handle_budget_pressure, priority=8)
        self.on("cuda_validated", self._handle_cuda_validated, priority=7)
        self.on("model_loaded", self._handle_model_loaded, priority=6)
        self.on("node_*", self._handle_node_event, priority=7)

    def on(
        self,
        pattern: str,
        handler: Callable,
        name: str = "",
        priority: int = 5,
    ) -> EventHandler:
        h = EventHandler(
            name=name or f"{pattern}_{len(self._handlers)}",
            pattern=pattern,
            handler=handler,
            priority=priority,
        )
        self._handlers.append(h)
        self._handlers.sort(key=lambda x: -x.priority)
        return h

    def off(self, name: str) -> bool:
        before = len(self._handlers)
        self._handlers = [h for h in self._handlers if h.name != name]
        return len(self._handlers) < before

    async def _dispatch(self, event: JarvisEvent):
        matched = [h for h in self._handlers if h.enabled and event.matches(h.pattern)]
        if not matched:
            return

        self._stats["processed"] += 1

        for handler in matched:
            try:
                result = handler.handler(event)
                if asyncio.iscoroutine(result):
                    await result
                handler.call_count += 1
                handler.last_called = time.time()
            except Exception as e:
                handler.error_count += 1
                self._stats["errors"] += 1
                log.error(f"Handler '{handler.name}' error on '{event.event}': {e}")
                # Send to DLQ
                if self.redis:
                    await self.redis.lpush(
                        DEAD_LETTER_KEY,
                        json.dumps(
                            {
                                "event": event.event,
                                "data": event.data,
                                "error": str(e),
                                "handler": handler.name,
                                "ts": time.time(),
                            }
                        ),
                    )
                    await self.redis.ltrim(DEAD_LETTER_KEY, 0, 999)
                    self._stats["dlq"] += 1

    # ── Built-in handlers ─────────────────────────────────────────────────────

    async def _handle_thermal(self, event: JarvisEvent):
        actions = event.data.get("actions", [])
        for a in actions:
            log.warning(
                f"[THERMAL] GPU{a.get('gpu')} {a.get('pl_old')}W→{a.get('pl_new')}W "
                f"({a.get('reason')})"
            )

    async def _handle_budget_pressure(self, event: JarvisEvent):
        session = event.data.get("session_id", "?")
        pressure = event.data.get("pressure", "?")
        pct = event.data.get("usage_pct", 0)
        log.warning(f"[BUDGET] {session}: {pressure} at {pct}%")

    async def _handle_cuda_validated(self, event: JarvisEvent):
        score = event.data.get("score", 0)
        gpu = event.data.get("gpu", "?")
        log.info(f"[CUDA] Validated {gpu}: score={score}")

    async def _handle_model_loaded(self, event: JarvisEvent):
        model = event.data.get("model", "?")
        node = event.data.get("node", "?")
        log.info(f"[MODEL] Loaded {model} on {node}")

    async def _handle_node_event(self, event: JarvisEvent):
        log.info(f"[NODE] {event.event}: {json.dumps(event.data)[:100]}")

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(self, channels: list[str] | None = None):
        if not self.redis:
            await self.connect_redis()
        if not self.redis:
            log.error("Cannot start — Redis unavailable")
            return

        channels = channels or [EVENTS_CHANNEL]
        self._running = True
        log.info(f"Event processor started, listening on: {channels}")

        async with self.redis.pubsub() as ps:
            await ps.subscribe(*channels)
            async for message in ps.listen():
                if not self._running:
                    break
                if message["type"] != "message":
                    continue

                self._stats["received"] += 1
                event = JarvisEvent.from_message(
                    message["data"],
                    channel=message["channel"],
                )
                if event:
                    await self._dispatch(event)

                # Save stats periodically
                if self._stats["received"] % 100 == 0:
                    await self._save_stats()

    def stop(self):
        self._running = False

    async def publish(self, event: str, **data) -> int:
        if not self.redis:
            return 0
        payload = {"event": event, "ts": time.time(), **data}
        return await self.redis.publish(EVENTS_CHANNEL, json.dumps(payload))

    async def _save_stats(self):
        if self.redis:
            await self.redis.set(
                STATS_KEY,
                json.dumps(
                    {
                        **self._stats,
                        "handlers": len(self._handlers),
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    }
                ),
                ex=300,
            )

    def handler_stats(self) -> list[dict]:
        return [
            {
                "name": h.name,
                "pattern": h.pattern,
                "calls": h.call_count,
                "errors": h.error_count,
                "priority": h.priority,
                "enabled": h.enabled,
            }
            for h in self._handlers
        ]


# ── CLI ───────────────────────────────────────────────────────────────────────


async def main():
    import sys

    proc = EventProcessor()
    await proc.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "listen"

    if cmd == "listen":
        print(f"Listening on {EVENTS_CHANNEL}...")
        await proc.run()

    elif cmd == "publish" and len(sys.argv) > 2:
        event = sys.argv[2]
        data = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
        n = await proc.publish(event, **data)
        print(f"Published '{event}' to {n} subscribers")

    elif cmd == "handlers":
        for h in proc.handler_stats():
            status = "✅" if h["enabled"] else "❌"
            print(
                f"{status} [{h['priority']:>2}] {h['name']:<30} "
                f"pattern={h['pattern']:<20} calls={h['calls']} err={h['errors']}"
            )

    elif cmd == "dlq":
        if proc.redis:
            items = await proc.redis.lrange(DEAD_LETTER_KEY, 0, 19)
            print(f"Dead letter queue ({len(items)} items):")
            for item in items:
                d = json.loads(item)
                print(
                    f"  [{d.get('event')}] handler={d.get('handler')} err={d.get('error')[:60]}"
                )

    elif cmd == "stats":
        if proc.redis:
            raw = await proc.redis.get(STATS_KEY)
            if raw:
                print(json.dumps(json.loads(raw), indent=2))
            else:
                print("No stats yet")


if __name__ == "__main__":
    asyncio.run(main())
