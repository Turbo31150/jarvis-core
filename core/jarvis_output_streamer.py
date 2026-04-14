#!/usr/bin/env python3
"""
jarvis_output_streamer — Server-Sent Events and WebSocket output streaming
Streams LLM token responses to multiple consumers with buffering and backpressure
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import AsyncIterator

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.output_streamer")

REDIS_CHANNEL = "jarvis:stream:tokens"
REDIS_PREFIX = "jarvis:stream:"


class StreamEvent(str, Enum):
    TOKEN = "token"
    DONE = "done"
    ERROR = "error"
    METADATA = "metadata"
    HEARTBEAT = "heartbeat"


@dataclass
class StreamChunk:
    stream_id: str
    event: StreamEvent
    data: str
    index: int = 0
    model: str = ""
    finish_reason: str = ""
    ts: float = field(default_factory=time.time)

    def to_sse(self) -> str:
        """Format as Server-Sent Event."""
        payload = json.dumps(
            {
                "stream_id": self.stream_id,
                "event": self.event.value,
                "data": self.data,
                "index": self.index,
                "model": self.model,
                "finish_reason": self.finish_reason,
            }
        )
        return f"event: {self.event.value}\ndata: {payload}\n\n"

    def to_dict(self) -> dict:
        return {
            "stream_id": self.stream_id,
            "event": self.event.value,
            "data": self.data,
            "index": self.index,
            "model": self.model,
            "finish_reason": self.finish_reason,
            "ts": self.ts,
        }


@dataclass
class StreamSession:
    stream_id: str
    model: str
    endpoint_url: str
    messages: list[dict]
    created_at: float = field(default_factory=time.time)
    token_count: int = 0
    finished: bool = False
    error: str = ""
    full_text: str = ""
    consumers: int = 0

    @property
    def duration_s(self) -> float:
        return time.time() - self.created_at

    @property
    def tokens_per_second(self) -> float:
        dur = self.duration_s
        return self.token_count / dur if dur > 0 else 0.0

    def to_dict(self) -> dict:
        return {
            "stream_id": self.stream_id,
            "model": self.model,
            "token_count": self.token_count,
            "finished": self.finished,
            "duration_s": round(self.duration_s, 2),
            "tokens_per_second": round(self.tokens_per_second, 1),
            "consumers": self.consumers,
            "error": self.error,
        }


async def _stream_lmstudio(
    endpoint_url: str,
    model: str,
    messages: list[dict],
    stream_id: str,
    max_tokens: int = 1024,
    temperature: float = 0.7,
    timeout_s: float = 60.0,
) -> AsyncIterator[StreamChunk]:
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
    }
    index = 0
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout_s)
        ) as sess:
            async with sess.post(
                f"{endpoint_url}/v1/chat/completions",
                json=payload,
            ) as r:
                if r.status != 200:
                    text = await r.text()
                    yield StreamChunk(
                        stream_id=stream_id,
                        event=StreamEvent.ERROR,
                        data=f"HTTP {r.status}: {text[:100]}",
                    )
                    return

                async for raw_line in r.content:
                    line = raw_line.decode("utf-8").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        yield StreamChunk(
                            stream_id=stream_id,
                            event=StreamEvent.DONE,
                            data="",
                            index=index,
                        )
                        return
                    try:
                        chunk_data = json.loads(data_str)
                        delta = (
                            chunk_data.get("choices", [{}])[0]
                            .get("delta", {})
                            .get("content", "")
                        )
                        finish = (
                            chunk_data.get("choices", [{}])[0].get("finish_reason")
                            or ""
                        )
                        if delta:
                            yield StreamChunk(
                                stream_id=stream_id,
                                event=StreamEvent.TOKEN,
                                data=delta,
                                index=index,
                                model=model,
                                finish_reason=finish,
                            )
                            index += 1
                        if finish and finish != "null":
                            yield StreamChunk(
                                stream_id=stream_id,
                                event=StreamEvent.DONE,
                                data="",
                                index=index,
                                finish_reason=finish,
                            )
                            return
                    except json.JSONDecodeError:
                        pass
    except Exception as e:
        yield StreamChunk(
            stream_id=stream_id,
            event=StreamEvent.ERROR,
            data=str(e)[:200],
        )


class OutputStreamer:
    def __init__(self, buffer_size: int = 1024):
        self.redis: aioredis.Redis | None = None
        self._sessions: dict[str, StreamSession] = {}
        self._queues: dict[
            str, list[asyncio.Queue]
        ] = {}  # stream_id → list of consumer queues
        self._buffer_size = buffer_size
        self._stats: dict[str, int] = {
            "streams_started": 0,
            "streams_completed": 0,
            "streams_errored": 0,
            "total_tokens": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def create_session(
        self,
        model: str,
        endpoint_url: str,
        messages: list[dict],
        stream_id: str | None = None,
    ) -> StreamSession:
        sid = stream_id or str(uuid.uuid4())[:12]
        session = StreamSession(
            stream_id=sid,
            model=model,
            endpoint_url=endpoint_url,
            messages=messages,
        )
        self._sessions[sid] = session
        self._queues[sid] = []
        return session

    def subscribe(self, stream_id: str, maxsize: int = 512) -> asyncio.Queue:
        """Subscribe to a stream, returns a queue that receives StreamChunk objects."""
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._queues.setdefault(stream_id, []).append(q)
        session = self._sessions.get(stream_id)
        if session:
            session.consumers += 1
        return q

    async def _broadcast(self, stream_id: str, chunk: StreamChunk):
        for q in self._queues.get(stream_id, []):
            try:
                q.put_nowait(chunk)
            except asyncio.QueueFull:
                pass  # backpressure: drop for slow consumer
        if self.redis:
            asyncio.create_task(
                self.redis.publish(REDIS_CHANNEL, json.dumps(chunk.to_dict()))
            )

    async def stream(
        self,
        stream_id: str,
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> StreamSession:
        session = self._sessions.get(stream_id)
        if not session:
            raise ValueError(f"No session: {stream_id}")

        self._stats["streams_started"] += 1
        log.info(f"Stream started: {stream_id} ({session.model})")

        async for chunk in _stream_lmstudio(
            session.endpoint_url,
            session.model,
            session.messages,
            stream_id,
            max_tokens,
            temperature,
        ):
            if chunk.event == StreamEvent.TOKEN:
                session.token_count += 1
                session.full_text += chunk.data
                self._stats["total_tokens"] += 1
            elif chunk.event == StreamEvent.ERROR:
                session.error = chunk.data
                session.finished = True
                self._stats["streams_errored"] += 1
            elif chunk.event == StreamEvent.DONE:
                session.finished = True
                self._stats["streams_completed"] += 1

            await self._broadcast(stream_id, chunk)

        # Signal all consumers that stream is done
        if not session.finished:
            session.finished = True
            done_chunk = StreamChunk(
                stream_id=stream_id, event=StreamEvent.DONE, data=""
            )
            await self._broadcast(stream_id, done_chunk)

        return session

    async def consume_to_text(self, stream_id: str) -> str:
        """Convenience: run stream and return full assembled text."""
        q = self.subscribe(stream_id)
        asyncio.create_task(self.stream(stream_id))
        text_parts: list[str] = []
        while True:
            chunk: StreamChunk = await q.get()
            if chunk.event == StreamEvent.TOKEN:
                text_parts.append(chunk.data)
            elif chunk.event in (StreamEvent.DONE, StreamEvent.ERROR):
                break
        return "".join(text_parts)

    def get_session(self, stream_id: str) -> StreamSession | None:
        return self._sessions.get(stream_id)

    def active_streams(self) -> list[dict]:
        return [s.to_dict() for s in self._sessions.values() if not s.finished]

    def stats(self) -> dict:
        return {
            **self._stats,
            "active_streams": len(self.active_streams()),
            "total_sessions": len(self._sessions),
        }


def build_jarvis_output_streamer() -> OutputStreamer:
    return OutputStreamer(buffer_size=2048)


async def main():
    import sys

    streamer = build_jarvis_output_streamer()
    await streamer.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        model = "qwen3.5-9b"
        endpoint = "http://192.168.1.85:1234"
        messages = [
            {"role": "user", "content": "Write a haiku about distributed systems."}
        ]

        session = streamer.create_session(model, endpoint, messages)
        print(f"Stream ID: {session.stream_id}")
        print("Tokens: ", end="", flush=True)

        q = streamer.subscribe(session.stream_id)
        stream_task = asyncio.create_task(streamer.stream(session.stream_id))

        token_count = 0
        while True:
            chunk: StreamChunk = await q.get()
            if chunk.event == StreamEvent.TOKEN:
                print(chunk.data, end="", flush=True)
                token_count += 1
            elif chunk.event in (StreamEvent.DONE, StreamEvent.ERROR):
                break

        await stream_task
        print(f"\n\nTokens: {token_count}, TPS: {session.tokens_per_second:.1f}")
        print(f"Stats: {json.dumps(streamer.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(streamer.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
