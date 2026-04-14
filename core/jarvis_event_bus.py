#!/usr/bin/env python3
"""JARVIS Event Bus — Async event routing with subscriptions and filters"""

import redis
import json
import threading
import time
from datetime import datetime
from typing import Callable

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:eventbus"

_subscriptions = {}
_lock = threading.Lock()


class EventBus:
    def __init__(self):
        self._subs = {}
        self._lock = threading.Lock()
        self._running = False
        self._thread = None

    def subscribe(self, event_type: str, handler: Callable, filter_fn: Callable = None):
        with self._lock:
            if event_type not in self._subs:
                self._subs[event_type] = []
            self._subs[event_type].append({"handler": handler, "filter": filter_fn})

    def unsubscribe(self, event_type: str):
        with self._lock:
            self._subs.pop(event_type, None)

    def publish(self, event_type: str, data: dict, severity: str = "info") -> int:
        event = {
            "type": event_type,
            "data": data,
            "severity": severity,
            "ts": datetime.now().isoformat()[:19],
        }
        # Redis pub/sub
        subscribers = r.publish("jarvis:events", json.dumps(event))
        # Also push to stream
        r.xadd("jarvis:stream", {"event": json.dumps(event)}, maxlen=1000)
        # Local handlers
        handlers_called = 0
        with self._lock:
            for handler_cfg in self._subs.get(event_type, []) + self._subs.get("*", []):
                try:
                    if handler_cfg["filter"] and not handler_cfg["filter"](event):
                        continue
                    handler_cfg["handler"](event)
                    handlers_called += 1
                except Exception:
                    pass
        # Stats
        r.hincrby(f"{PREFIX}:stats:{event_type}", "published", 1)
        return subscribers + handlers_called

    def start_listener(self, channels: list = None):
        channels = channels or ["jarvis:events", "jarvis:alerts"]
        def _listen():
            pub = r.pubsub()
            pub.subscribe(*channels)
            self._running = True
            for msg in pub.listen():
                if not self._running:
                    break
                if msg["type"] == "message":
                    try:
                        event = json.loads(msg["data"])
                        etype = event.get("type", "unknown")
                        with self._lock:
                            for handler_cfg in self._subs.get(etype, []) + self._subs.get("*", []):
                                try:
                                    handler_cfg["handler"](event)
                                except Exception:
                                    pass
                    except Exception:
                        pass
        self._thread = threading.Thread(target=_listen, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def stats(self) -> dict:
        result = {}
        for key in r.scan_iter(f"{PREFIX}:stats:*"):
            etype = key.replace(f"{PREFIX}:stats:", "")
            data = r.hgetall(key)
            result[etype] = int(data.get("published", 0))
        return {"subscriptions": list(self._subs.keys()), "published_by_type": result}


# Singleton instance
bus = EventBus()


if __name__ == "__main__":
    received = []
    bus.subscribe("test_event", lambda e: received.append(e))
    bus.subscribe("*", lambda e: print(f"  [*] {e['type']}: {e['data']}"))

    bus.publish("test_event", {"message": "hello"}, "info")
    bus.publish("alert_event", {"cpu": 95}, "warning")
    bus.publish("test_event", {"message": "world"}, "info")

    print(f"Test events received: {len(received)}")
    print(f"Stats: {bus.stats()}")
