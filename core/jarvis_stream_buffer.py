#!/usr/bin/env python3
"""
jarvis_stream_buffer — Circular buffer for LLM token streams with replay and backpressure
Accumulates streamed tokens, supports multiple consumers, windowed replay
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.stream_buffer")

REDIS_PREFIX = "jarvis:streambuf:"


class BufferState(str, Enum):
    OPEN = "open"  # actively receiving tokens
    DONE = "done"  # stream completed
    ERROR = "error"  # stream errored
    CANCELLED = "cancelled"


@dataclass
class TokenChunk:
    index: int
    token: str
    ts: float = field(default_factory=time.time)
    is_final: bool = False
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "token": self.token,
            "ts": self.ts,
            "is_final": self.is_final,
        }


@dataclass
class ConsumerCursor:
    consumer_id: str
    position: int = 0  # next index to read
    created_at: float = field(default_factory=time.time)
    last_read: float = field(default_factory=time.time)
    tokens_read: int = 0


class StreamBuffer:
    """Single-stream buffer — one producer, multiple async consumers."""

    def __init__(
        self,
        stream_id: str,
        max_size: int = 4096,
        backpressure_threshold: int = 2048,
    ):
        self.stream_id = stream_id
        self._max_size = max_size
        self._backpressure_threshold = backpressure_threshold
        self._chunks: list[TokenChunk] = []
        self._state = BufferState.OPEN
        self._consumers: dict[str, ConsumerCursor] = {}
        self._new_data = asyncio.Event()
        self._error: str = ""
        self._stats = {
            "chunks_written": 0,
            "chunks_read": 0,
            "backpressure_events": 0,
            "consumers_added": 0,
        }

    @property
    def state(self) -> BufferState:
        return self._state

    @property
    def size(self) -> int:
        return len(self._chunks)

    @property
    def full_text(self) -> str:
        return "".join(c.token for c in self._chunks)

    def write(
        self, token: str, metadata: dict | None = None, is_final: bool = False
    ) -> bool:
        """Write a token. Returns False if buffer is full (backpressure)."""
        if self._state != BufferState.OPEN:
            return False
        if len(self._chunks) >= self._max_size:
            self._stats["backpressure_events"] += 1
            return False

        chunk = TokenChunk(
            index=len(self._chunks),
            token=token,
            metadata=metadata or {},
            is_final=is_final,
        )
        self._chunks.append(chunk)
        self._stats["chunks_written"] += 1
        self._new_data.set()

        if is_final:
            self._state = BufferState.DONE

        return True

    def finish(self, error: str = ""):
        if error:
            self._state = BufferState.ERROR
            self._error = error
        else:
            self._state = BufferState.DONE
        self._new_data.set()

    def add_consumer(self, consumer_id: str, replay_from: int = 0) -> ConsumerCursor:
        cursor = ConsumerCursor(
            consumer_id=consumer_id,
            position=max(0, min(replay_from, len(self._chunks))),
        )
        self._consumers[consumer_id] = cursor
        self._stats["consumers_added"] += 1
        return cursor

    def remove_consumer(self, consumer_id: str):
        self._consumers.pop(consumer_id, None)

    async def read(
        self, consumer_id: str, timeout_s: float = 30.0
    ) -> TokenChunk | None:
        """Read next chunk for consumer, waiting if none available."""
        cursor = self._consumers.get(consumer_id)
        if not cursor:
            return None

        deadline = time.time() + timeout_s
        while True:
            if cursor.position < len(self._chunks):
                chunk = self._chunks[cursor.position]
                cursor.position += 1
                cursor.last_read = time.time()
                cursor.tokens_read += 1
                self._stats["chunks_read"] += 1
                return chunk

            if self._state in (
                BufferState.DONE,
                BufferState.ERROR,
                BufferState.CANCELLED,
            ):
                return None

            remaining = deadline - time.time()
            if remaining <= 0:
                return None

            self._new_data.clear()
            try:
                await asyncio.wait_for(
                    self._new_data.wait(), timeout=min(remaining, 1.0)
                )
            except asyncio.TimeoutError:
                pass

    async def read_all(self, consumer_id: str, timeout_s: float = 60.0):
        """Async generator yielding all chunks for a consumer."""
        self.add_consumer(consumer_id)
        try:
            while True:
                chunk = await self.read(consumer_id, timeout_s=timeout_s)
                if chunk is None:
                    break
                yield chunk
        finally:
            self.remove_consumer(consumer_id)

    def peek(self, start: int = 0, end: int | None = None) -> list[TokenChunk]:
        return self._chunks[start:end]

    def slowest_consumer(self) -> str | None:
        if not self._consumers:
            return None
        return min(self._consumers, key=lambda cid: self._consumers[cid].position)

    def backpressure_needed(self) -> bool:
        slowest = self.slowest_consumer()
        if not slowest:
            return False
        lag = len(self._chunks) - self._consumers[slowest].position
        return lag >= self._backpressure_threshold

    def consumer_stats(self) -> list[dict]:
        return [
            {
                "consumer_id": c.consumer_id,
                "position": c.position,
                "lag": len(self._chunks) - c.position,
                "tokens_read": c.tokens_read,
            }
            for c in self._consumers.values()
        ]

    def stats(self) -> dict:
        return {
            **self._stats,
            "state": self._state.value,
            "size": self.size,
            "consumers": len(self._consumers),
            "backpressure": self.backpressure_needed(),
        }


class StreamBufferRegistry:
    """Manages multiple named stream buffers."""

    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._buffers: dict[str, StreamBuffer] = {}
        self._stats = {"created": 0, "finished": 0, "active": 0}

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def create(
        self,
        stream_id: str,
        max_size: int = 4096,
    ) -> StreamBuffer:
        buf = StreamBuffer(stream_id, max_size=max_size)
        self._buffers[stream_id] = buf
        self._stats["created"] += 1
        self._stats["active"] += 1
        return buf

    def get(self, stream_id: str) -> StreamBuffer | None:
        return self._buffers.get(stream_id)

    def finish(self, stream_id: str, error: str = ""):
        buf = self._buffers.get(stream_id)
        if buf:
            buf.finish(error)
            self._stats["finished"] += 1
            self._stats["active"] = max(0, self._stats["active"] - 1)

    def evict_done(self, max_age_s: float = 300.0):
        now = time.time()
        to_remove = []
        for sid, buf in self._buffers.items():
            if buf.state in (BufferState.DONE, BufferState.ERROR):
                age = now - (buf._chunks[-1].ts if buf._chunks else 0)
                if age > max_age_s:
                    to_remove.append(sid)
        for sid in to_remove:
            del self._buffers[sid]

    def list_buffers(self) -> list[dict]:
        return [{"stream_id": sid, **buf.stats()} for sid, buf in self._buffers.items()]

    def stats(self) -> dict:
        return {**self._stats, "buffers": len(self._buffers)}


def build_jarvis_stream_registry() -> StreamBufferRegistry:
    return StreamBufferRegistry()


async def main():
    import sys

    registry = build_jarvis_stream_registry()
    await registry.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        buf = registry.create("stream_001")

        # Simulate two consumers
        async def consumer(name: str, delay: float):
            chunks_read = []
            async for chunk in buf.read_all(name):
                chunks_read.append(chunk.token)
                await asyncio.sleep(delay)
            text = "".join(chunks_read)
            print(f"  Consumer {name}: {len(chunks_read)} chunks → '{text[:60]}'")

        # Producer
        async def producer():
            tokens = "The quick brown fox jumps over the lazy dog".split()
            for i, token in enumerate(tokens):
                buf.write(token + " ", is_final=(i == len(tokens) - 1))
                await asyncio.sleep(0.01)
            registry.finish("stream_001")

        print("Starting stream demo...")
        await asyncio.gather(
            producer(),
            consumer("reader_A", 0.005),
            consumer("reader_B", 0.02),
        )

        print(f"\nFull text: '{buf.full_text[:80]}'")
        print(f"Stats: {json.dumps(buf.stats(), indent=2)}")
        print(f"Registry: {json.dumps(registry.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(registry.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
