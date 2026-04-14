#!/usr/bin/env python3
"""
jarvis_stream_aggregator — SSE/streaming response aggregation and fan-out
Merges multiple streaming LLM responses, aggregates tokens, fans out to subscribers
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

log = logging.getLogger("jarvis.stream_aggregator")

REDIS_PREFIX = "jarvis:stream:"


@dataclass
class StreamChunk:
    stream_id: str
    source: str
    content: str
    finish_reason: str = ""
    ts: float = field(default_factory=time.time)
    index: int = 0

    def to_dict(self) -> dict:
        return {
            "stream_id": self.stream_id,
            "source": self.source,
            "content": self.content,
            "finish_reason": self.finish_reason,
            "index": self.index,
        }


@dataclass
class StreamResult:
    stream_id: str
    source: str
    full_text: str
    chunk_count: int
    duration_ms: float
    tokens_out: int
    finish_reason: str = "stop"
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "stream_id": self.stream_id,
            "source": self.source,
            "full_text": self.full_text[:500],
            "chunk_count": self.chunk_count,
            "duration_ms": round(self.duration_ms, 1),
            "tokens_out": self.tokens_out,
            "finish_reason": self.finish_reason,
            "error": self.error,
        }


class StreamAggregator:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._active: dict[str, asyncio.Queue] = {}  # stream_id → chunk queue
        self._subscribers: dict[
            str, list[asyncio.Queue]
        ] = {}  # stream_id → subscriber queues
        self._results: dict[str, StreamResult] = {}
        self._stats: dict[str, int] = {
            "streams_started": 0,
            "streams_completed": 0,
            "total_chunks": 0,
            "total_chars": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    async def consume_sse(
        self,
        url: str,
        payload: dict,
        source: str = "llm",
        stream_id: str | None = None,
    ) -> StreamResult:
        """Connect to SSE endpoint and collect all chunks."""
        sid = stream_id or str(uuid.uuid4())[:10]
        self._active[sid] = asyncio.Queue()
        self._stats["streams_started"] += 1
        t0 = time.time()
        chunks = []
        full_text = ""
        finish_reason = "stop"
        error = ""
        index = 0

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=120)
            ) as sess:
                async with sess.post(url, json=payload) as resp:
                    if resp.status != 200:
                        error = f"HTTP {resp.status}"
                        raise ConnectionError(error)

                    async for raw_line in resp.content:
                        line = raw_line.decode().strip()
                        if not line or not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                            delta = data.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            fr = data.get("choices", [{}])[0].get("finish_reason") or ""
                            if content:
                                chunk = StreamChunk(
                                    stream_id=sid,
                                    source=source,
                                    content=content,
                                    finish_reason=fr,
                                    index=index,
                                )
                                chunks.append(chunk)
                                full_text += content
                                index += 1
                                self._stats["total_chunks"] += 1
                                self._stats["total_chars"] += len(content)

                                # Fan-out to subscribers
                                for sq in self._subscribers.get(sid, []):
                                    await sq.put(chunk)

                                if fr:
                                    finish_reason = fr
                        except json.JSONDecodeError:
                            pass

        except Exception as e:
            error = str(e)[:200]
            log.error(f"Stream {sid} error: {e}")

        # Signal subscribers done
        sentinel = StreamChunk(
            stream_id=sid, source=source, content="", finish_reason="__done__"
        )
        for sq in self._subscribers.get(sid, []):
            await sq.put(sentinel)

        result = StreamResult(
            stream_id=sid,
            source=source,
            full_text=full_text,
            chunk_count=len(chunks),
            duration_ms=(time.time() - t0) * 1000,
            tokens_out=len(full_text.split()),
            finish_reason=finish_reason,
            error=error,
        )
        self._results[sid] = result
        self._active.pop(sid, None)
        self._stats["streams_completed"] += 1

        if self.redis:
            asyncio.create_task(
                self.redis.setex(
                    f"{REDIS_PREFIX}{sid}",
                    3600,
                    json.dumps(result.to_dict()),
                )
            )
        return result

    def subscribe(self, stream_id: str) -> asyncio.Queue:
        """Get a queue that receives StreamChunk objects as they arrive."""
        q: asyncio.Queue = asyncio.Queue()
        if stream_id not in self._subscribers:
            self._subscribers[stream_id] = []
        self._subscribers[stream_id].append(q)
        return q

    def unsubscribe(self, stream_id: str, queue: asyncio.Queue):
        subs = self._subscribers.get(stream_id, [])
        if queue in subs:
            subs.remove(queue)

    async def stream_iter(self, stream_id: str) -> AsyncIterator[StreamChunk]:
        """Async iterator over chunks for a given stream."""
        q = self.subscribe(stream_id)
        try:
            while True:
                chunk = await q.get()
                if chunk.finish_reason == "__done__":
                    break
                yield chunk
        finally:
            self.unsubscribe(stream_id, q)

    async def fan_out(
        self,
        url: str,
        payload: dict,
        n_subscribers: int = 1,
    ) -> tuple[str, list[asyncio.Queue]]:
        """Start a stream and return (stream_id, subscriber_queues)."""
        sid = str(uuid.uuid4())[:10]
        queues = [self.subscribe(sid) for _ in range(n_subscribers)]
        asyncio.create_task(self.consume_sse(url, payload, stream_id=sid))
        return sid, queues

    def get_result(self, stream_id: str) -> StreamResult | None:
        return self._results.get(stream_id)

    def stats(self) -> dict:
        return {
            **self._stats,
            "active_streams": len(self._active),
            "cached_results": len(self._results),
        }


async def main():
    import sys

    agg = StreamAggregator()
    await agg.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        # Mock SSE server
        async def mock_sse_handler(request):
            async def gen():
                for i, word in enumerate(
                    "Hello world from JARVIS streaming test".split()
                ):
                    chunk = {
                        "choices": [
                            {"delta": {"content": word + " "}, "finish_reason": None}
                        ]
                    }
                    yield (f"data: {json.dumps(chunk)}\n\n").encode()
                    await asyncio.sleep(0.01)
                yield b"data: [DONE]\n\n"

            from aiohttp import web

            return web.Response(
                body=gen(),
                content_type="text/event-stream",
            )

        from aiohttp import web

        app = web.Application()
        app.router.add_post("/v1/chat/completions", mock_sse_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 19999)
        await site.start()

        payload = {
            "model": "test",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }
        result = await agg.consume_sse(
            "http://127.0.0.1:19999/v1/chat/completions", payload
        )
        print(
            f"Stream [{result.stream_id}]: '{result.full_text.strip()}' ({result.chunk_count} chunks, {result.duration_ms:.0f}ms)"
        )
        print(f"Stats: {json.dumps(agg.stats(), indent=2)}")
        await runner.cleanup()

    elif cmd == "stats":
        print(json.dumps(agg.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
