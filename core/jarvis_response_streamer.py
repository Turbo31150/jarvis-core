#!/usr/bin/env python3
"""
jarvis_response_streamer — SSE/chunked streaming from LLM backends with fan-out
Streams LLM tokens to multiple consumers with buffering, backpressure, and reconnect
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import AsyncIterator

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.response_streamer")

REDIS_PREFIX = "jarvis:stream:"


class StreamState(str, Enum):
    IDLE = "idle"
    STREAMING = "streaming"
    COMPLETE = "complete"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass
class StreamChunk:
    stream_id: str
    index: int
    content: str
    finish_reason: str = ""
    ts: float = field(default_factory=time.time)
    is_final: bool = False

    def to_dict(self) -> dict:
        return {
            "stream_id": self.stream_id,
            "index": self.index,
            "content": self.content,
            "finish_reason": self.finish_reason,
            "is_final": self.is_final,
        }

    def to_sse(self) -> str:
        return f"data: {json.dumps(self.to_dict())}\n\n"


@dataclass
class StreamSession:
    stream_id: str
    backend: str
    model: str
    state: StreamState = StreamState.IDLE
    chunks: list[StreamChunk] = field(default_factory=list)
    consumers: int = 0
    created_at: float = field(default_factory=time.time)
    completed_at: float = 0.0
    error: str = ""
    total_tokens: int = 0

    @property
    def full_content(self) -> str:
        return "".join(c.content for c in self.chunks)

    @property
    def duration_ms(self) -> float:
        end = self.completed_at if self.completed_at > 0 else time.time()
        return (end - self.created_at) * 1000

    def to_dict(self) -> dict:
        return {
            "stream_id": self.stream_id,
            "backend": self.backend,
            "model": self.model,
            "state": self.state.value,
            "chunks": len(self.chunks),
            "total_tokens": self.total_tokens,
            "consumers": self.consumers,
            "duration_ms": round(self.duration_ms, 1),
            "error": self.error,
        }


class ResponseStreamer:
    def __init__(self, buffer_size: int = 64, max_sessions: int = 100):
        self.redis: aioredis.Redis | None = None
        self._sessions: dict[str, StreamSession] = {}
        self._queues: dict[
            str, list[asyncio.Queue]
        ] = {}  # stream_id → [consumer queues]
        self._buffer_size = buffer_size
        self._max_sessions = max_sessions
        self._stats: dict[str, int] = {
            "streams_started": 0,
            "streams_completed": 0,
            "streams_errored": 0,
            "total_chunks": 0,
            "consumers_attached": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def _new_stream_id(self) -> str:
        import uuid

        return str(uuid.uuid4())[:12]

    def _evict_old(self):
        if len(self._sessions) < self._max_sessions:
            return
        # Remove oldest completed sessions
        completed = [
            (sid, s)
            for sid, s in self._sessions.items()
            if s.state
            in (StreamState.COMPLETE, StreamState.ERROR, StreamState.CANCELLED)
        ]
        completed.sort(key=lambda x: x[1].created_at)
        for sid, _ in completed[: len(completed) // 2]:
            del self._sessions[sid]
            self._queues.pop(sid, None)

    async def stream(
        self,
        messages: list[dict],
        backend_url: str,
        model: str,
        params: dict | None = None,
    ) -> str:
        """Start a stream, return stream_id."""
        self._evict_old()
        stream_id = self._new_stream_id()
        session = StreamSession(
            stream_id=stream_id,
            backend=backend_url,
            model=model,
        )
        self._sessions[stream_id] = session
        self._queues[stream_id] = []
        self._stats["streams_started"] += 1

        asyncio.create_task(
            self._do_stream(session, messages, backend_url, model, params or {})
        )
        return stream_id

    async def _do_stream(
        self,
        session: StreamSession,
        messages: list[dict],
        url: str,
        model: str,
        params: dict,
    ):
        session.state = StreamState.STREAMING
        idx = 0
        try:
            payload = {
                "model": model,
                "messages": messages,
                "stream": True,
                **params,
            }
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=120)
            ) as http:
                async with http.post(f"{url}/v1/chat/completions", json=payload) as r:
                    if r.status != 200:
                        raise RuntimeError(f"HTTP {r.status}")

                    async for raw_line in r.content:
                        line = raw_line.decode().strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            obj = json.loads(data)
                            delta = obj["choices"][0].get("delta", {})
                            content = delta.get("content", "")
                            finish = obj["choices"][0].get("finish_reason", "")
                            if not content and not finish:
                                continue

                            chunk = StreamChunk(
                                stream_id=session.stream_id,
                                index=idx,
                                content=content,
                                finish_reason=finish or "",
                                is_final=bool(finish),
                            )
                            session.chunks.append(chunk)
                            session.total_tokens += 1
                            idx += 1
                            self._stats["total_chunks"] += 1

                            # Fan-out to all consumer queues
                            for q in self._queues.get(session.stream_id, []):
                                try:
                                    q.put_nowait(chunk)
                                except asyncio.QueueFull:
                                    pass  # backpressure — drop chunk for this consumer

                            if self.redis:
                                await self.redis.rpush(
                                    f"{REDIS_PREFIX}{session.stream_id}",
                                    json.dumps(chunk.to_dict()),
                                )

                        except json.JSONDecodeError:
                            pass

            # Send final sentinel
            final = StreamChunk(session.stream_id, idx, "", "stop", is_final=True)
            session.chunks.append(final)
            for q in self._queues.get(session.stream_id, []):
                try:
                    q.put_nowait(final)
                except asyncio.QueueFull:
                    pass

            session.state = StreamState.COMPLETE
            session.completed_at = time.time()
            self._stats["streams_completed"] += 1

        except asyncio.CancelledError:
            session.state = StreamState.CANCELLED
        except Exception as e:
            session.error = str(e)[:100]
            session.state = StreamState.ERROR
            self._stats["streams_errored"] += 1
            # Notify consumers of error
            err_chunk = StreamChunk(session.stream_id, idx, "", "error", is_final=True)
            for q in self._queues.get(session.stream_id, []):
                try:
                    q.put_nowait(err_chunk)
                except asyncio.QueueFull:
                    pass

    async def subscribe(self, stream_id: str) -> AsyncIterator[StreamChunk]:
        """Async generator that yields chunks for a stream."""
        self._stats["consumers_attached"] += 1
        session = self._sessions.get(stream_id)
        if not session:
            return

        q: asyncio.Queue = asyncio.Queue(maxsize=self._buffer_size)
        queues = self._queues.setdefault(stream_id, [])
        queues.append(q)
        session.consumers += 1

        # Replay buffered chunks first
        for chunk in session.chunks:
            yield chunk
            if chunk.is_final:
                queues.remove(q)
                session.consumers -= 1
                return

        # Stream live chunks
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(q.get(), timeout=60.0)
                    yield chunk
                    if chunk.is_final:
                        break
                except asyncio.TimeoutError:
                    break
        finally:
            if q in queues:
                queues.remove(q)
            session.consumers = max(0, session.consumers - 1)

    async def collect(self, stream_id: str) -> str:
        """Wait for stream to complete and return full text."""
        chunks = []
        async for chunk in self.subscribe(stream_id):
            if chunk.content:
                chunks.append(chunk.content)
        return "".join(chunks)

    def cancel(self, stream_id: str):
        session = self._sessions.get(stream_id)
        if session:
            session.state = StreamState.CANCELLED

    def get_session(self, stream_id: str) -> StreamSession | None:
        return self._sessions.get(stream_id)

    def list_sessions(self) -> list[dict]:
        return [s.to_dict() for s in self._sessions.values()]

    def stats(self) -> dict:
        active = sum(
            1 for s in self._sessions.values() if s.state == StreamState.STREAMING
        )
        return {
            **self._stats,
            "active_streams": active,
            "total_sessions": len(self._sessions),
        }


def build_jarvis_streamer() -> ResponseStreamer:
    return ResponseStreamer(buffer_size=128, max_sessions=200)


async def main():
    import sys

    streamer = build_jarvis_streamer()
    await streamer.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        messages = [
            {
                "role": "user",
                "content": "Count from 1 to 5 with a word after each number.",
            }
        ]
        backend = "http://192.168.1.85:1234"
        model = "qwen3.5-9b"

        print("Starting stream...")
        stream_id = await streamer.stream(messages, backend, model)
        print(f"Stream ID: {stream_id}")

        print("Collecting:")
        try:
            text = await asyncio.wait_for(streamer.collect(stream_id), timeout=30)
            print(f"  Result: {text[:200]}")
        except asyncio.TimeoutError:
            print("  Timeout — backend likely unavailable in demo")

        print(f"\nStats: {json.dumps(streamer.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(streamer.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
