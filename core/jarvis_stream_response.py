#!/usr/bin/env python3
"""
jarvis_stream_response — SSE streaming response handler for LLM APIs
Handles server-sent events, token-by-token streaming, progress tracking
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import AsyncIterator

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.stream_response")

REDIS_STREAM_KEY = "jarvis:stream:"
DEFAULT_URL = "http://127.0.0.1:1234"


@dataclass
class StreamChunk:
    content: str
    token_idx: int
    finish_reason: str | None = None
    ts: float = field(default_factory=time.time)


@dataclass
class StreamSession:
    session_id: str
    model: str
    prompt: str
    chunks: list[StreamChunk] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    finish_reason: str = ""
    error: str = ""

    @property
    def full_text(self) -> str:
        return "".join(c.content for c in self.chunks)

    @property
    def token_count(self) -> int:
        return len(self.chunks)

    @property
    def duration_s(self) -> float:
        end = self.finished_at or time.time()
        return round(end - self.started_at, 3)

    @property
    def tps(self) -> float:
        d = self.duration_s
        return round(self.token_count / d, 1) if d > 0 else 0.0


class StreamResponseHandler:
    def __init__(self, base_url: str = DEFAULT_URL):
        self.base_url = base_url
        self.redis: aioredis.Redis | None = None
        self._active: dict[str, StreamSession] = {}

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    async def stream(
        self,
        prompt: str,
        model: str = "qwen/qwen3.5-9b",
        max_tokens: int = 512,
        temperature: float = 0.0,
        session_id: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Yield StreamChunks as they arrive from SSE stream."""
        import uuid

        sid = session_id or str(uuid.uuid4())[:8]
        session = StreamSession(session_id=sid, model=model, prompt=prompt[:200])
        self._active[sid] = session
        token_idx = 0

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=180, connect=5)
            ) as sess:
                async with sess.post(
                    f"{self.base_url}/v1/chat/completions",
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                        "stream": True,
                    },
                ) as response:
                    if response.status != 200:
                        err = await response.text()
                        session.error = f"HTTP {response.status}: {err[:100]}"
                        return

                    async for line_bytes in response.content:
                        line = line_bytes.decode("utf-8").strip()
                        if not line:
                            continue
                        if line.startswith("data: "):
                            line = line[6:]
                        if line == "[DONE]":
                            break

                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        choices = data.get("choices", [])
                        if not choices:
                            continue

                        delta = choices[0].get("delta", {})
                        finish = choices[0].get("finish_reason")
                        content = delta.get("content", "")

                        if content:
                            chunk = StreamChunk(
                                content=content,
                                token_idx=token_idx,
                                finish_reason=finish,
                            )
                            session.chunks.append(chunk)
                            token_idx += 1

                            # Publish to Redis for monitoring
                            if self.redis and token_idx % 10 == 0:
                                await self.redis.set(
                                    f"{REDIS_STREAM_KEY}{sid}",
                                    json.dumps(
                                        {
                                            "tokens": token_idx,
                                            "tps": session.tps,
                                            "preview": session.full_text[-100:],
                                        }
                                    ),
                                    ex=60,
                                )

                            yield chunk

                        if finish:
                            session.finish_reason = finish

        except Exception as e:
            session.error = str(e)
            log.error(f"Stream error [{sid}]: {e}")
        finally:
            session.finished_at = time.time()
            self._active.pop(sid, None)

    async def stream_to_string(
        self,
        prompt: str,
        model: str = "qwen/qwen3.5-9b",
        max_tokens: int = 512,
        temperature: float = 0.0,
        callback=None,
    ) -> tuple[str, StreamSession]:
        """Stream and collect all tokens into a string."""
        import uuid

        sid = str(uuid.uuid4())[:8]
        session = StreamSession(session_id=sid, model=model, prompt=prompt[:200])

        # Re-run with our session tracking
        parts = []
        async for chunk in self.stream(prompt, model, max_tokens, temperature, sid):
            parts.append(chunk.content)
            if callback:
                try:
                    await callback(chunk)
                except Exception:
                    pass

        session.chunks = [
            StreamChunk(content=p, token_idx=i) for i, p in enumerate(parts)
        ]
        session.finished_at = time.time()
        return "".join(parts), session

    def active_streams(self) -> list[dict]:
        return [
            {
                "session_id": s.session_id,
                "model": s.model,
                "tokens": s.token_count,
                "tps": s.tps,
                "age_s": round(time.time() - s.started_at, 1),
            }
            for s in self._active.values()
        ]


async def main():
    import sys

    handler = StreamResponseHandler()
    await handler.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stream"

    if cmd == "stream" and len(sys.argv) > 2:
        prompt = " ".join(sys.argv[2:])
        model = "qwen/qwen3.5-9b"
        print(f"Streaming [{model}]: {prompt[:60]}")
        print("-" * 60)

        t0 = time.time()
        token_count = 0

        async for chunk in handler.stream(prompt, model, max_tokens=300):
            print(chunk.content, end="", flush=True)
            token_count += 1

        elapsed = time.time() - t0
        print(
            f"\n\n[{token_count} tokens | {token_count / elapsed:.1f} tok/s | {elapsed:.2f}s]"
        )

    elif cmd == "collect" and len(sys.argv) > 2:
        prompt = " ".join(sys.argv[2:])
        text, session = await handler.stream_to_string(prompt)
        print(text)
        print(
            f"\n[{session.token_count} tokens | {session.tps} tok/s | {session.duration_s:.2f}s]"
        )

    elif cmd == "active":
        streams = handler.active_streams()
        if not streams:
            print("No active streams")
        for s in streams:
            print(
                f"  [{s['session_id']}] {s['model']} | {s['tokens']} tokens | {s['tps']} tok/s"
            )


if __name__ == "__main__":
    asyncio.run(main())
