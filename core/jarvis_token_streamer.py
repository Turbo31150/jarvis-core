#!/usr/bin/env python3
"""
jarvis_token_streamer — High-performance token streaming with backpressure control
Streams LLM tokens to multiple subscribers with buffering, replay, and flow control
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import AsyncIterator

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.token_streamer")

LM_URL = "http://127.0.0.1:1234"
REDIS_PREFIX = "jarvis:stream:"
MAX_BUFFER_SIZE = 500
BACKPRESSURE_THRESHOLD = 200  # pause producing if buffer exceeds this


@dataclass
class Token:
    index: int
    text: str
    ts: float = field(default_factory=time.time)
    is_done: bool = False

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "text": self.text,
            "ts": self.ts,
            "done": self.is_done,
        }


@dataclass
class StreamSession:
    session_id: str
    model: str
    prompt: str
    tokens: list[Token] = field(default_factory=list)
    status: str = "streaming"  # streaming | done | error | cancelled
    started_at: float = field(default_factory=time.time)
    ended_at: float = 0.0
    subscribers: int = 0
    error: str = ""

    @property
    def full_text(self) -> str:
        return "".join(t.text for t in self.tokens if not t.is_done)

    @property
    def token_count(self) -> int:
        return sum(1 for t in self.tokens if not t.is_done)

    @property
    def tok_per_s(self) -> float:
        elapsed = (self.ended_at or time.time()) - self.started_at
        return round(self.token_count / max(elapsed, 0.001), 1)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "model": self.model,
            "status": self.status,
            "token_count": self.token_count,
            "tok_per_s": self.tok_per_s,
            "subscribers": self.subscribers,
            "started_at": self.started_at,
        }


class TokenStreamer:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._sessions: dict[str, StreamSession] = {}
        self._queues: dict[
            str, list[asyncio.Queue]
        ] = {}  # session_id → [subscriber queues]

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    async def create_stream(
        self,
        prompt: str,
        model: str = "qwen/qwen3.5-9b",
        messages: list[dict] | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        system: str = "",
    ) -> str:
        """Start a streaming session. Returns session_id."""
        session_id = str(uuid.uuid4())[:8]
        msgs = messages or []
        if not msgs:
            if system:
                msgs.append({"role": "system", "content": system})
            msgs.append({"role": "user", "content": prompt})

        session = StreamSession(session_id=session_id, model=model, prompt=prompt)
        self._sessions[session_id] = session
        self._queues[session_id] = []

        # Launch background producer
        asyncio.create_task(self._produce(session, msgs, max_tokens, temperature))
        log.debug(f"Stream created: [{session_id}] model={model}")
        return session_id

    async def _produce(
        self,
        session: StreamSession,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
    ):
        idx = 0
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=120)
            ) as http:
                async with http.post(
                    f"{LM_URL}/v1/chat/completions",
                    json={
                        "model": session.model,
                        "messages": messages,
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                        "stream": True,
                    },
                ) as r:
                    if r.status != 200:
                        session.status = "error"
                        session.error = f"HTTP {r.status}"
                        await self._broadcast(
                            session.session_id, Token(idx, "", is_done=True)
                        )
                        return

                    async for line_bytes in r.content:
                        line = line_bytes.decode().strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                            text = (
                                chunk["choices"][0].get("delta", {}).get("content", "")
                            )
                            if text:
                                token = Token(index=idx, text=text)
                                session.tokens.append(token)
                                await self._broadcast(session.session_id, token)
                                idx += 1

                                # Backpressure: if buffer growing too large, yield
                                if idx % 10 == 0:
                                    for q in self._queues[session.session_id]:
                                        if q.qsize() > BACKPRESSURE_THRESHOLD:
                                            await asyncio.sleep(0.05)
                        except Exception:
                            pass

        except Exception as e:
            session.status = "error"
            session.error = str(e)
            log.error(f"Stream error [{session.session_id}]: {e}")

        # Send done sentinel
        done_token = Token(index=idx, text="", is_done=True)
        session.tokens.append(done_token)
        session.status = "done"
        session.ended_at = time.time()
        await self._broadcast(session.session_id, done_token)

        # Redis: store completed session
        if self.redis:
            await self.redis.setex(
                f"{REDIS_PREFIX}{session.session_id}",
                3600,
                json.dumps(
                    {**session.to_dict(), "full_text": session.full_text[:2000]}
                ),
            )

        log.info(
            f"Stream done [{session.session_id}]: {session.token_count}t {session.tok_per_s:.1f}tok/s"
        )

    async def _broadcast(self, session_id: str, token: Token):
        queues = self._queues.get(session_id, [])
        for q in queues:
            try:
                q.put_nowait(token)
            except asyncio.QueueFull:
                log.warning(f"Subscriber queue full for [{session_id}]")

    async def subscribe(self, session_id: str) -> AsyncIterator[Token]:
        """Subscribe to a stream and yield tokens as they arrive."""
        session = self._sessions.get(session_id)
        if not session:
            raise ValueError(f"Session '{session_id}' not found")

        q: asyncio.Queue[Token] = asyncio.Queue(maxsize=MAX_BUFFER_SIZE)
        self._queues[session_id].append(q)
        session.subscribers += 1

        # Replay existing tokens for late subscribers
        for token in session.tokens:
            await q.put(token)

        try:
            while True:
                token = await asyncio.wait_for(q.get(), timeout=60.0)
                yield token
                if token.is_done:
                    break
        except asyncio.TimeoutError:
            log.warning(f"Subscriber timeout for [{session_id}]")
        finally:
            self._queues[session_id].remove(q)
            session.subscribers -= 1

    async def get_replay(self, session_id: str) -> list[Token]:
        """Get all tokens from a completed session."""
        session = self._sessions.get(session_id)
        if session:
            return session.tokens
        if self.redis:
            raw = await self.redis.get(f"{REDIS_PREFIX}{session_id}")
            if raw:
                d = json.loads(raw)
                # Reconstruct from full_text
                text = d.get("full_text", "")
                return [Token(0, text, is_done=False), Token(1, "", is_done=True)]
        return []

    def cancel(self, session_id: str):
        session = self._sessions.get(session_id)
        if session and session.status == "streaming":
            session.status = "cancelled"
            asyncio.create_task(
                self._broadcast(session_id, Token(-1, "", is_done=True))
            )

    def stats(self) -> dict:
        sessions = list(self._sessions.values())
        return {
            "total_sessions": len(sessions),
            "active": sum(1 for s in sessions if s.status == "streaming"),
            "done": sum(1 for s in sessions if s.status == "done"),
            "total_tokens": sum(s.token_count for s in sessions),
            "avg_tok_per_s": round(
                sum(s.tok_per_s for s in sessions if s.tok_per_s > 0)
                / max(sum(1 for s in sessions if s.tok_per_s > 0), 1),
                1,
            ),
        }


async def main():
    import sys

    streamer = TokenStreamer()
    await streamer.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        prompt = sys.argv[2] if len(sys.argv) > 2 else "Count to 5 slowly."
        print(f"Streaming: '{prompt}'")
        session_id = await streamer.create_stream(prompt, max_tokens=100)
        full = ""
        async for token in streamer.subscribe(session_id):
            if token.is_done:
                break
            print(token.text, end="", flush=True)
            full += token.text
        print()
        session = streamer._sessions[session_id]
        print(
            f"\n[{session_id}] {session.token_count} tokens at {session.tok_per_s:.1f} tok/s"
        )

    elif cmd == "stats":
        print(json.dumps(streamer.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
