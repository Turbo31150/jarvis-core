#!/usr/bin/env python3
"""
jarvis_batch_processor — Batched LLM request processing
Queues prompts, groups by model, dispatches optimally to reduce overhead
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.batch_processor")

REDIS_QUEUE = "jarvis:batch:queue"
REDIS_RESULTS = "jarvis:batch:results:"
RESULT_TTL = 300  # 5 min
MAX_BATCH_SIZE = 8
BATCH_WINDOW_MS = 50  # collect requests for 50ms before firing

BACKENDS = {
    "M1": "http://127.0.0.1:1234",
    "M2": "http://192.168.1.26:1234",
}


@dataclass
class BatchRequest:
    req_id: str
    model: str
    messages: list[dict]
    max_tokens: int = 512
    temperature: float = 0.0
    priority: int = 5  # 1-10, lower = higher priority
    created_at: float = field(default_factory=time.time)

    def to_payload(self) -> dict:
        return {
            "model": self.model,
            "messages": self.messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }


@dataclass
class BatchResult:
    req_id: str
    content: str
    tokens_used: int
    latency_s: float
    backend: str
    error: str | None = None


class BatchProcessor:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._pending: dict[str, asyncio.Future] = {}
        self._running = False
        self._stats = {
            "total": 0,
            "ok": 0,
            "errors": 0,
            "batches_fired": 0,
            "avg_batch_size": 0.0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    async def submit(
        self,
        prompt: str,
        model: str = "qwen/qwen3.5-9b",
        max_tokens: int = 512,
        temperature: float = 0.0,
        priority: int = 5,
    ) -> str:
        """Submit a request, returns req_id. Use await_result(req_id) to get response."""
        req_id = str(uuid.uuid4())[:8]
        req = BatchRequest(
            req_id=req_id,
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=temperature,
            priority=priority,
        )
        # Priority queue: (priority, time, request)
        await self._queue.put((priority, time.time(), req))
        self._pending[req_id] = asyncio.get_event_loop().create_future()
        return req_id

    async def await_result(
        self, req_id: str, timeout: float = 120.0
    ) -> BatchResult | None:
        fut = self._pending.get(req_id)
        if not fut:
            # Try Redis
            if self.redis:
                raw = await self.redis.get(f"{REDIS_RESULTS}{req_id}")
                if raw:
                    d = json.loads(raw)
                    return BatchResult(**d)
            return None
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            return None

    async def _fire_batch(self, batch: list[BatchRequest], backend_url: str):
        """Fire a batch of requests concurrently to a backend."""
        tasks = [self._fire_one(req, backend_url) for req in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for req, result in zip(batch, results):
            if isinstance(result, Exception):
                r = BatchResult(
                    req_id=req.req_id,
                    content="",
                    tokens_used=0,
                    latency_s=0,
                    backend=backend_url,
                    error=str(result),
                )
                self._stats["errors"] += 1
            else:
                r = result
                self._stats["ok"] += 1

            self._stats["total"] += 1

            # Resolve future
            fut = self._pending.pop(req.req_id, None)
            if fut and not fut.done():
                fut.set_result(r)

            # Store in Redis
            if self.redis:
                await self.redis.set(
                    f"{REDIS_RESULTS}{req.req_id}",
                    json.dumps(
                        {
                            "req_id": r.req_id,
                            "content": r.content,
                            "tokens_used": r.tokens_used,
                            "latency_s": r.latency_s,
                            "backend": r.backend,
                            "error": r.error,
                        }
                    ),
                    ex=RESULT_TTL,
                )

    async def _fire_one(self, req: BatchRequest, backend_url: str) -> BatchResult:
        url = f"{backend_url}/v1/chat/completions"
        t0 = time.time()
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=120)
        ) as sess:
            async with sess.post(url, json=req.to_payload()) as r:
                data = await r.json()

        elapsed = time.time() - t0
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        tokens = data.get("usage", {}).get("total_tokens", 0)

        return BatchResult(
            req_id=req.req_id,
            content=content,
            tokens_used=tokens,
            latency_s=round(elapsed, 3),
            backend=backend_url,
        )

    def _best_backend(self, model: str) -> str:
        m2_models = ["35b", "deepseek", "glm", "gpt-oss", "nemotron"]
        if any(k in model.lower() for k in m2_models):
            return BACKENDS["M2"]
        return BACKENDS["M1"]

    async def run_loop(self):
        self._running = True
        log.info("Batch processor started")

        while self._running:
            # Collect batch within window
            batch: list[BatchRequest] = []
            deadline = time.time() + BATCH_WINDOW_MS / 1000

            while len(batch) < MAX_BATCH_SIZE and time.time() < deadline:
                try:
                    _, _, req = await asyncio.wait_for(
                        self._queue.get(),
                        timeout=max(0.001, deadline - time.time()),
                    )
                    batch.append(req)
                except asyncio.TimeoutError:
                    break

            if not batch:
                await asyncio.sleep(0.01)
                continue

            # Group by model/backend
            by_backend: dict[str, list[BatchRequest]] = {}
            for req in batch:
                be = self._best_backend(req.model)
                by_backend.setdefault(be, []).append(req)

            tasks = [self._fire_batch(reqs, be) for be, reqs in by_backend.items()]
            await asyncio.gather(*tasks, return_exceptions=True)

            self._stats["batches_fired"] += 1
            n = self._stats["batches_fired"]
            self._stats["avg_batch_size"] = round(
                (self._stats["avg_batch_size"] * (n - 1) + len(batch)) / n, 2
            )

    def stop(self):
        self._running = False

    def stats(self) -> dict:
        return {**self._stats, "queue_depth": self._queue.qsize()}


# ── CLI ───────────────────────────────────────────────────────────────────────


async def main():
    import sys

    proc = BatchProcessor()
    await proc.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "test"

    if cmd == "test":
        # Submit 5 requests and wait for results
        asyncio.create_task(proc.run_loop())
        await asyncio.sleep(0.1)

        prompts = [
            "Donne un fait sur Python",
            "Donne un fait sur Redis",
            "Donne un fait sur CUDA",
            "Donne un fait sur les LLM",
            "Donne un fait sur les GPU",
        ]

        req_ids = []
        for p in prompts:
            rid = await proc.submit(p, max_tokens=60)
            req_ids.append(rid)
            print(f"Submitted {rid}: {p[:40]}")

        print(f"\nWaiting for {len(req_ids)} results...")
        for rid in req_ids:
            result = await proc.await_result(rid, timeout=60)
            if result:
                status = "✅" if not result.error else "❌"
                print(
                    f"  {status} {rid}: {result.latency_s:.2f}s "
                    f"[{result.backend.split('/')[-1]}] "
                    f"{result.content[:80] if result.content else result.error}"
                )

        proc.stop()
        print(f"\nStats: {json.dumps(proc.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(proc.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
