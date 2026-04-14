#!/usr/bin/env python3
"""
jarvis_session_replay — Session recording and deterministic replay
Records LLM interactions and system events; replays for debugging and testing
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.session_replay")

REPLAY_DIR = Path("/home/turbo/IA/Core/jarvis/data/sessions")
REDIS_PREFIX = "jarvis:session:"


@dataclass
class SessionEvent:
    seq: int
    event_type: str  # request | response | tool_call | tool_result | system
    data: dict
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "event_type": self.event_type,
            "data": self.data,
            "ts": self.ts,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SessionEvent":
        return cls(
            seq=d["seq"],
            event_type=d["event_type"],
            data=d["data"],
            ts=d.get("ts", 0.0),
        )


@dataclass
class Session:
    session_id: str
    name: str
    events: list[SessionEvent] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    ended_at: float = 0.0
    status: str = "recording"  # recording | done | replaying

    @property
    def duration_s(self) -> float:
        end = self.ended_at or time.time()
        return round(end - self.started_at, 2)

    @property
    def request_count(self) -> int:
        return sum(1 for e in self.events if e.event_type == "request")

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "name": self.name,
            "status": self.status,
            "event_count": len(self.events),
            "request_count": self.request_count,
            "duration_s": self.duration_s,
            "started_at": self.started_at,
            "metadata": self.metadata,
        }


class SessionRecorder:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._sessions: dict[str, Session] = {}
        REPLAY_DIR.mkdir(parents=True, exist_ok=True)

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def start(self, name: str = "", **meta) -> Session:
        session = Session(
            session_id=str(uuid.uuid4())[:8],
            name=name or f"session_{int(time.time())}",
            metadata=meta,
        )
        self._sessions[session.session_id] = session
        log.debug(f"Recording started: [{session.session_id}] {session.name}")
        return session

    def record(self, session_id: str, event_type: str, data: dict) -> SessionEvent:
        session = self._sessions.get(session_id)
        if not session:
            raise ValueError(f"Session '{session_id}' not found")
        event = SessionEvent(seq=len(session.events), event_type=event_type, data=data)
        session.events.append(event)
        return event

    def record_request(
        self, session_id: str, model: str, messages: list, **kwargs
    ) -> SessionEvent:
        return self.record(
            session_id, "request", {"model": model, "messages": messages, **kwargs}
        )

    def record_response(
        self, session_id: str, content: str, latency_ms: float, **kwargs
    ) -> SessionEvent:
        return self.record(
            session_id,
            "response",
            {"content": content, "latency_ms": latency_ms, **kwargs},
        )

    def record_tool_call(self, session_id: str, tool: str, args: dict) -> SessionEvent:
        return self.record(session_id, "tool_call", {"tool": tool, "args": args})

    def record_tool_result(
        self, session_id: str, tool: str, result: Any
    ) -> SessionEvent:
        return self.record(
            session_id, "tool_result", {"tool": tool, "result": str(result)[:500]}
        )

    def stop(self, session_id: str) -> Session:
        session = self._sessions.get(session_id)
        if not session:
            raise ValueError(f"Session '{session_id}' not found")
        session.status = "done"
        session.ended_at = time.time()
        self._save(session)
        log.info(
            f"Recording stopped: [{session_id}] {len(session.events)} events in {session.duration_s}s"
        )
        return session

    def _save(self, session: Session):
        path = REPLAY_DIR / f"{session.session_id}.jsonl"
        with open(path, "w") as f:
            f.write(json.dumps(session.to_dict()) + "\n")
            for event in session.events:
                f.write(json.dumps(event.to_dict()) + "\n")

    def load(self, session_id: str) -> Session | None:
        path = REPLAY_DIR / f"{session_id}.jsonl"
        if not path.exists():
            return None
        lines = path.read_text().strip().split("\n")
        meta = json.loads(lines[0])
        session = Session(
            session_id=meta["session_id"],
            name=meta["name"],
            metadata=meta.get("metadata", {}),
            started_at=meta["started_at"],
            ended_at=meta.get("ended_at", 0.0),
            status=meta.get("status", "done"),
        )
        for line in lines[1:]:
            if line.strip():
                session.events.append(SessionEvent.from_dict(json.loads(line)))
        self._sessions[session_id] = session
        return session

    def list_sessions(self) -> list[dict]:
        result = []
        for path in sorted(
            REPLAY_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
        ):
            try:
                first_line = path.read_text().split("\n")[0]
                result.append(json.loads(first_line))
            except Exception:
                pass
        return result

    async def replay(
        self,
        session_id: str,
        handler: Any = None,  # async callable(event) → response
        speed: float = 1.0,  # 1.0=real-time, 0=instant
    ) -> list[dict]:
        session = self._sessions.get(session_id) or self.load(session_id)
        if not session:
            raise ValueError(f"Session '{session_id}' not found or not saved")

        session.status = "replaying"
        results = []
        prev_ts = session.events[0].ts if session.events else time.time()

        for event in session.events:
            if speed > 0:
                delay = (event.ts - prev_ts) / speed
                if delay > 0:
                    await asyncio.sleep(min(delay, 5.0))
            prev_ts = event.ts

            if handler and event.event_type == "request":
                try:
                    resp = await handler(event)
                    results.append(
                        {"seq": event.seq, "replayed": True, "response": resp}
                    )
                except Exception as e:
                    results.append(
                        {"seq": event.seq, "replayed": False, "error": str(e)}
                    )
            else:
                results.append({"seq": event.seq, "event_type": event.event_type})

        session.status = "done"
        log.info(f"Replay done: [{session_id}] {len(results)} events processed")
        return results

    def diff(self, session_id_a: str, session_id_b: str) -> dict:
        """Compare two sessions event by event."""
        a = self._sessions.get(session_id_a) or self.load(session_id_a)
        b = self._sessions.get(session_id_b) or self.load(session_id_b)
        if not a or not b:
            return {"error": "Session not found"}

        diffs = []
        max_len = max(len(a.events), len(b.events))
        for i in range(max_len):
            ea = a.events[i] if i < len(a.events) else None
            eb = b.events[i] if i < len(b.events) else None
            if ea is None or eb is None:
                diffs.append({"seq": i, "diff": "length_mismatch"})
            elif ea.event_type != eb.event_type:
                diffs.append(
                    {
                        "seq": i,
                        "diff": "type_mismatch",
                        "a": ea.event_type,
                        "b": eb.event_type,
                    }
                )
            elif ea.data != eb.data:
                diffs.append({"seq": i, "diff": "data_changed"})
        return {
            "session_a": session_id_a,
            "session_b": session_id_b,
            "total_events_a": len(a.events),
            "total_events_b": len(b.events),
            "diffs": len(diffs),
            "details": diffs[:20],
        }

    def stats(self) -> dict:
        sessions = list(self._sessions.values())
        return {
            "loaded": len(sessions),
            "saved": len(list(REPLAY_DIR.glob("*.jsonl"))),
            "total_events": sum(len(s.events) for s in sessions),
            "avg_duration_s": round(
                sum(s.duration_s for s in sessions) / max(len(sessions), 1), 2
            ),
        }


async def main():
    import sys

    recorder = SessionRecorder()
    await recorder.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        session = recorder.start("demo_session", user="turbo")
        recorder.record_request(
            session.session_id,
            "qwen3.5-9b",
            [{"role": "user", "content": "What is Redis?"}],
        )
        await asyncio.sleep(0.1)
        recorder.record_response(
            session.session_id, "Redis is an in-memory store.", 243.0
        )
        recorder.record_tool_call(session.session_id, "redis_get", {"key": "foo"})
        recorder.record_tool_result(session.session_id, "redis_get", "bar")
        recorder.stop(session.session_id)

        s = recorder._sessions[session.session_id]
        print(
            f"Session [{s.session_id}]: {s.event_count if hasattr(s, 'event_count') else len(s.events)} events, {s.duration_s}s"
        )
        for e in s.events:
            print(f"  [{e.seq}] {e.event_type}: {str(e.data)[:60]}")

    elif cmd == "list":
        sessions = recorder.list_sessions()
        print(f"{'ID':<10} {'Name':<30} {'Events':>7} {'Duration':>10}")
        print("-" * 60)
        for s in sessions[:20]:
            print(
                f"  {s['session_id']:<10} {s['name']:<30} {s['event_count']:>7} {s['duration_s']:>9.1f}s"
            )

    elif cmd == "stats":
        print(json.dumps(recorder.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
