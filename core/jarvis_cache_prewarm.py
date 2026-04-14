#!/usr/bin/env python3
"""
jarvis_cache_prewarm — Proactive cache warming for LLM prompts and embeddings
Pre-populates Redis caches with likely queries to reduce cold-start latency
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.cache_prewarm")

REDIS_PREFIX = "jarvis:prewarm:"
WARMUP_FILE = Path("/home/turbo/IA/Core/jarvis/data/warmup_queries.json")


class WarmupStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"  # already cached


@dataclass
class WarmupEntry:
    entry_id: str
    query: str
    cache_key: str
    warmup_type: str  # embed | prompt | model
    priority: int = 5
    ttl_s: int = 3600
    status: WarmupStatus = WarmupStatus.PENDING
    latency_ms: float = 0.0
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "entry_id": self.entry_id,
            "query": self.query[:80],
            "cache_key": self.cache_key,
            "type": self.warmup_type,
            "status": self.status.value,
            "latency_ms": round(self.latency_ms, 1),
            "error": self.error,
        }


@dataclass
class WarmupResult:
    total: int
    done: int
    skipped: int
    failed: int
    duration_ms: float
    entries: list[WarmupEntry]

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "done": self.done,
            "skipped": self.skipped,
            "failed": self.failed,
            "duration_ms": round(self.duration_ms, 1),
            "success_rate": round(self.done / max(self.total, 1), 3),
        }


class CachePrewarmer:
    def __init__(
        self,
        embed_url: str = "http://192.168.1.26:1234",
        embed_model: str = "nomic-embed-text",
        llm_url: str = "http://192.168.1.85:1234",
        llm_model: str = "qwen3.5-9b",
        concurrency: int = 4,
    ):
        self.redis: aioredis.Redis | None = None
        self._embed_url = embed_url
        self._embed_model = embed_model
        self._llm_url = llm_url
        self._llm_model = llm_model
        self._concurrency = concurrency
        self._queue: list[WarmupEntry] = []
        self._custom_warmers: dict[str, Callable] = {}
        self._stats: dict[str, int] = {
            "warmed": 0,
            "skipped": 0,
            "failed": 0,
            "runs": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def add_embed_query(self, query: str, priority: int = 5, ttl_s: int = 3600):
        """Schedule an embedding to be pre-warmed."""
        import hashlib

        cache_key = (
            f"{REDIS_PREFIX}embed:{hashlib.md5(query.encode()).hexdigest()[:16]}"
        )
        entry_id = f"e{len(self._queue):04d}"
        self._queue.append(
            WarmupEntry(entry_id, query, cache_key, "embed", priority, ttl_s)
        )

    def add_prompt_query(
        self, prompt: str, system: str = "", priority: int = 5, ttl_s: int = 1800
    ):
        """Schedule a prompt response to be pre-warmed."""
        import hashlib

        key_str = f"{system}|{prompt}"
        cache_key = (
            f"{REDIS_PREFIX}prompt:{hashlib.md5(key_str.encode()).hexdigest()[:16]}"
        )
        entry_id = f"p{len(self._queue):04d}"
        entry = WarmupEntry(entry_id, prompt, cache_key, "prompt", priority, ttl_s)
        entry.__dict__["system"] = system
        self._queue.append(entry)

    def add_custom(
        self,
        entry_id: str,
        query: str,
        cache_key: str,
        warmer_fn: Callable,
        priority: int = 5,
    ):
        """Register a custom warmup entry with a user-provided function."""
        self._custom_warmers[entry_id] = warmer_fn
        self._queue.append(WarmupEntry(entry_id, query, cache_key, "custom", priority))

    def load_from_file(self) -> int:
        if not WARMUP_FILE.exists():
            return 0
        try:
            data = json.loads(WARMUP_FILE.read_text())
            count = 0
            for item in data.get("embed", []):
                self.add_embed_query(
                    item["query"], item.get("priority", 5), item.get("ttl_s", 3600)
                )
                count += 1
            for item in data.get("prompts", []):
                self.add_prompt_query(
                    item["prompt"], item.get("system", ""), item.get("priority", 5)
                )
                count += 1
            return count
        except Exception as e:
            log.warning(f"Warmup file load error: {e}")
            return 0

    async def _is_cached(self, cache_key: str) -> bool:
        if not self.redis:
            return False
        return await self.redis.exists(cache_key) > 0

    async def _warm_embed(self, entry: WarmupEntry):
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            ) as sess:
                async with sess.post(
                    f"{self._embed_url}/v1/embeddings",
                    json={"model": self._embed_model, "input": entry.query[:2048]},
                ) as r:
                    if r.status == 200:
                        data = await r.json(content_type=None)
                        emb = data["data"][0]["embedding"]
                        if self.redis:
                            await self.redis.setex(
                                entry.cache_key, entry.ttl_s, json.dumps(emb)
                            )
                        return True
                    return False
        except Exception as e:
            entry.error = str(e)[:80]
            return False

    async def _warm_prompt(self, entry: WarmupEntry):
        system = entry.__dict__.get("system", "")
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": entry.query})
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            ) as sess:
                async with sess.post(
                    f"{self._llm_url}/v1/chat/completions",
                    json={
                        "model": self._llm_model,
                        "messages": messages,
                        "max_tokens": 512,
                    },
                ) as r:
                    if r.status == 200:
                        data = await r.json(content_type=None)
                        content = data["choices"][0]["message"]["content"]
                        if self.redis:
                            await self.redis.setex(
                                entry.cache_key,
                                entry.ttl_s,
                                json.dumps({"content": content}),
                            )
                        return True
                    return False
        except Exception as e:
            entry.error = str(e)[:80]
            return False

    async def _process_entry(self, entry: WarmupEntry):
        t0 = time.time()

        if await self._is_cached(entry.cache_key):
            entry.status = WarmupStatus.SKIPPED
            self._stats["skipped"] += 1
            return

        entry.status = WarmupStatus.RUNNING
        success = False

        if entry.warmup_type == "embed":
            success = await self._warm_embed(entry)
        elif entry.warmup_type == "prompt":
            success = await self._warm_prompt(entry)
        elif entry.warmup_type == "custom" and entry.entry_id in self._custom_warmers:
            try:
                await self._custom_warmers[entry.entry_id](entry)
                success = True
            except Exception as e:
                entry.error = str(e)[:80]

        entry.latency_ms = (time.time() - t0) * 1000
        if success:
            entry.status = WarmupStatus.DONE
            self._stats["warmed"] += 1
        else:
            entry.status = WarmupStatus.FAILED
            self._stats["failed"] += 1

    async def warm(self, priority_threshold: int = 0) -> WarmupResult:
        self._stats["runs"] += 1
        t0 = time.time()

        pending = sorted(
            [
                e
                for e in self._queue
                if e.status == WarmupStatus.PENDING and e.priority >= priority_threshold
            ],
            key=lambda e: -e.priority,
        )

        sem = asyncio.Semaphore(self._concurrency)

        async def bounded(entry: WarmupEntry):
            async with sem:
                await self._process_entry(entry)

        await asyncio.gather(*[bounded(e) for e in pending])

        done = sum(1 for e in self._queue if e.status == WarmupStatus.DONE)
        skipped = sum(1 for e in self._queue if e.status == WarmupStatus.SKIPPED)
        failed = sum(1 for e in self._queue if e.status == WarmupStatus.FAILED)

        return WarmupResult(
            total=len(pending),
            done=done,
            skipped=skipped,
            failed=failed,
            duration_ms=(time.time() - t0) * 1000,
            entries=self._queue,
        )

    def clear_queue(self):
        self._queue.clear()

    def queue_size(self) -> int:
        return len(self._queue)

    def stats(self) -> dict:
        return {**self._stats, "queue_size": self.queue_size()}


def build_jarvis_prewarmer() -> CachePrewarmer:
    pw = CachePrewarmer()
    # Common cluster queries
    pw.add_embed_query("GPU cluster health status", priority=9)
    pw.add_embed_query("what models are available on M1", priority=8)
    pw.add_embed_query("Redis connection error troubleshooting", priority=7)
    pw.add_embed_query("Python asyncio best practices", priority=6)
    pw.add_embed_query("VRAM allocation for LLM inference", priority=7)

    # Common prompts
    pw.add_prompt_query(
        "What is the status of the JARVIS cluster?",
        system="You are JARVIS, a GPU cluster AI assistant.",
        priority=8,
    )
    pw.add_prompt_query(
        "List the available LLM models and their capabilities.",
        system="You are JARVIS.",
        priority=7,
    )
    return pw


async def main():
    import sys

    pw = build_jarvis_prewarmer()
    await pw.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print(f"Queue: {pw.queue_size()} entries")
        print("Warming cache...")
        result = await pw.warm()
        print(
            f"Done: {result.done}  Skipped: {result.skipped}  Failed: {result.failed}"
        )
        print(f"Duration: {result.duration_ms:.0f}ms")
        for e in result.entries[:5]:
            icon = {"done": "✅", "skipped": "⏭️", "failed": "❌", "pending": "⏳"}.get(
                e.status.value, "?"
            )
            print(f"  {icon} [{e.warmup_type:<6}] {e.query[:60]}")
        print(f"\nStats: {json.dumps(pw.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(pw.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
