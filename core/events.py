"""JARVIS TaskBus — Internal event system with SQLite persistence."""
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "jarvis-master.db"

# Known event types
EVENT_TYPES = (
    "task.started", "task.completed", "task.failed",
    "agent.registered",
    "service.down", "service.up",
)


@dataclass
class Event:
    event_type: str
    source: str
    payload: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


class TaskBus:
    """Thread-safe pub/sub event bus with SQLite persistence."""

    def __init__(self, db_path: Optional[Path] = None):
        self._lock = threading.Lock()
        self._listeners: Dict[str, List[Callable]] = {}
        self._recent: List[Event] = []
        self._db_path = db_path or _DB_PATH
        self._ensure_table()

    def _ensure_table(self):
        try:
            with sqlite3.connect(str(self._db_path)) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_type TEXT NOT NULL,
                        source TEXT NOT NULL,
                        payload TEXT,
                        timestamp REAL NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_events_type
                    ON events(event_type)
                """)
        except sqlite3.Error:
            pass

    def subscribe(self, event_type: str, callback: Callable[[Event], None]):
        """Register a listener for an event type."""
        with self._lock:
            self._listeners.setdefault(event_type, []).append(callback)

    def publish(self, event_type: str, source: str,
                payload: Optional[Dict[str, Any]] = None) -> Event:
        """Broadcast an event to all listeners and persist to SQLite."""
        evt = Event(event_type=event_type, source=source,
                    payload=payload or {})
        with self._lock:
            self._recent.append(evt)
            if len(self._recent) > 500:
                self._recent = self._recent[-500:]
            listeners = list(self._listeners.get(event_type, []))

        # Persist
        try:
            import json
            with sqlite3.connect(str(self._db_path)) as conn:
                conn.execute(
                    "INSERT INTO events (event_type, source, payload, timestamp) "
                    "VALUES (?, ?, ?, ?)",
                    (evt.event_type, evt.source,
                     json.dumps(evt.payload), evt.timestamp),
                )
        except sqlite3.Error:
            pass

        # Notify listeners (outside lock)
        for cb in listeners:
            try:
                cb(evt)
            except Exception:
                pass

        return evt

    def history(self, limit: int = 50) -> List[Event]:
        """Return recent events from memory."""
        with self._lock:
            return list(self._recent[-limit:])

    def history_db(self, limit: int = 50, event_type: Optional[str] = None) -> List[Dict]:
        """Query persisted events from SQLite."""
        import json
        query = "SELECT event_type, source, payload, timestamp FROM events"
        params: list = []
        if event_type:
            query += " WHERE event_type = ?"
            params.append(event_type)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        try:
            with sqlite3.connect(str(self._db_path)) as conn:
                rows = conn.execute(query, params).fetchall()
            return [
                {"event_type": r[0], "source": r[1],
                 "payload": json.loads(r[2]) if r[2] else {}, "timestamp": r[3]}
                for r in rows
            ]
        except sqlite3.Error:
            return []


# Module-level singleton
bus = TaskBus()

if __name__ == "__main__":
    collected = []
    bus.subscribe("task.started", lambda e: collected.append(e))
    bus.publish("task.started", "demo", {"task": "test-event-bus"})
    bus.publish("task.completed", "demo", {"result": "ok"})
    bus.publish("service.up", "m3", {"model": "deepseek-r1-qwen3-8b"})

    print("=== In-memory history ===")
    for e in bus.history():
        print(f"  [{e.event_type}] {e.source}: {e.payload}")

    print(f"\nListeners captured: {len(collected)} event(s)")
    print(f"DB history: {len(bus.history_db())} event(s)")
