#!/usr/bin/env python3
"""
jarvis_inference_cache — Two-level inference result cache
L1 in-memory LRU + L2 Redis with TTL, semantic near-miss detection
"""

import asyncio
import hashlib
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.inference_cache")

REDIS_PREFIX = "jarvis:infer_cache:"
EMBED_URL = "http://192.168.1.26:1234"
EMBED_MODEL = "nomic-embed-text"

L1_MAX_SIZE = 512
L1_DEFAULT_TTL_S = 300
L2_DEFAULT_TTL_S = 3600
SEMANTIC_THRESHOLD = 0.95


def _cache_key(model: str, messages: list, params: dict) -> str:
    payload = {"model": model, "messages": messages, "params": params}
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


@dataclass
class CacheEntry:
    key: str
    model: str
    response: str
    tokens_used: int
    latency_ms: float
    hits: int = 0
    created_at: float = field(default_factory=time.time)
    last_hit: float = 0.0
    embedding: list[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "model": self.model,
            "response": self.response,
            "tokens_used": self.tokens_used,
            "latency_ms": self.latency_ms,
            "hits": self.hits,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CacheEntry":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class CacheStats:
    l1_hits: int = 0
    l2_hits: int = 0
    semantic_hits: int = 0
    misses: int = 0
    sets: int = 0
    evictions: int = 0

    @property
    def total_requests(self) -> int:
        return self.l1_hits + self.l2_hits + self.semantic_hits + self.misses

    @property
    def hit_rate(self) -> float:
        total = self.total_requests
        hits = self.l1_hits + self.l2_hits + self.semantic_hits
        return round(hits / max(total, 1), 3)

    def to_dict(self) -> dict:
        return {
            "l1_hits": self.l1_hits,
            "l2_hits": self.l2_hits,
            "semantic_hits": self.semantic_hits,
            "misses": self.misses,
            "sets": self.sets,
            "evictions": self.evictions,
            "total_requests": self.total_requests,
            "hit_rate": self.hit_rate,
        }


def _cosine_sim(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = sum(x * x for x in a) ** 0.5
    mag_b = sum(x * x for x in b) ** 0.5
    return dot / max(mag_a * mag_b, 1e-9)


class InferenceCache:
    def __init__(
        self,
        l1_max: int = L1_MAX_SIZE,
        l1_ttl_s: int = L1_DEFAULT_TTL_S,
        l2_ttl_s: int = L2_DEFAULT_TTL_S,
        semantic: bool = False,
    ):
        self.redis: aioredis.Redis | None = None
        self.l1_max = l1_max
        self.l1_ttl_s = l1_ttl_s
        self.l2_ttl_s = l2_ttl_s
        self.semantic = semantic
        self._l1: OrderedDict[str, tuple[CacheEntry, float]] = (
            OrderedDict()
        )  # key → (entry, expires)
        self._semantic_index: list[tuple[str, list[float]]] = []  # (key, embedding)
        self.stats = CacheStats()

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    async def _embed(self, text: str) -> list[float]:
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5)
            ) as sess:
                async with sess.post(
                    f"{EMBED_URL}/v1/embeddings",
                    json={"model": EMBED_MODEL, "input": text[:512]},
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        return data["data"][0]["embedding"]
        except Exception:
            pass
        return []

    def _l1_get(self, key: str) -> CacheEntry | None:
        item = self._l1.get(key)
        if not item:
            return None
        entry, expires = item
        if time.time() > expires:
            del self._l1[key]
            return None
        # Move to end (LRU)
        self._l1.move_to_end(key)
        entry.hits += 1
        entry.last_hit = time.time()
        return entry

    def _l1_set(self, key: str, entry: CacheEntry):
        if key in self._l1:
            self._l1.move_to_end(key)
        else:
            if len(self._l1) >= self.l1_max:
                self._l1.popitem(last=False)
                self.stats.evictions += 1
        self._l1[key] = (entry, time.time() + self.l1_ttl_s)

    async def get(
        self,
        model: str,
        messages: list,
        params: dict | None = None,
        prompt_text: str = "",
    ) -> CacheEntry | None:
        key = _cache_key(model, messages, params or {})

        # L1
        entry = self._l1_get(key)
        if entry:
            self.stats.l1_hits += 1
            return entry

        # L2 Redis
        if self.redis:
            raw = await self.redis.get(f"{REDIS_PREFIX}{key}")
            if raw:
                entry = CacheEntry.from_dict(json.loads(raw))
                self._l1_set(key, entry)
                self.stats.l2_hits += 1
                return entry

        # Semantic near-miss
        if self.semantic and prompt_text and self._semantic_index:
            query_emb = await self._embed(prompt_text)
            if query_emb:
                best_key, best_sim = "", 0.0
                for sk, semb in self._semantic_index:
                    sim = _cosine_sim(query_emb, semb)
                    if sim > best_sim:
                        best_sim, best_key = sim, sk
                if best_sim >= SEMANTIC_THRESHOLD:
                    entry = self._l1_get(best_key)
                    if entry:
                        self.stats.semantic_hits += 1
                        log.debug(f"Semantic cache hit: sim={best_sim:.3f}")
                        return entry

        self.stats.misses += 1
        return None

    async def set(
        self,
        model: str,
        messages: list,
        response: str,
        tokens_used: int = 0,
        latency_ms: float = 0.0,
        params: dict | None = None,
        prompt_text: str = "",
    ) -> CacheEntry:
        key = _cache_key(model, messages, params or {})
        entry = CacheEntry(
            key=key,
            model=model,
            response=response,
            tokens_used=tokens_used,
            latency_ms=latency_ms,
        )

        self._l1_set(key, entry)

        if self.redis:
            await self.redis.setex(
                f"{REDIS_PREFIX}{key}",
                self.l2_ttl_s,
                json.dumps(entry.to_dict()),
            )

        if self.semantic and prompt_text:
            emb = await self._embed(prompt_text)
            if emb:
                entry.embedding = emb
                self._semantic_index.append((key, emb))
                if len(self._semantic_index) > L1_MAX_SIZE:
                    self._semantic_index.pop(0)

        self.stats.sets += 1
        return entry

    async def invalidate(self, model: str, messages: list, params: dict | None = None):
        key = _cache_key(model, messages, params or {})
        self._l1.pop(key, None)
        if self.redis:
            await self.redis.delete(f"{REDIS_PREFIX}{key}")

    def l1_size(self) -> int:
        now = time.time()
        expired = [k for k, (_, exp) in self._l1.items() if exp < now]
        for k in expired:
            del self._l1[k]
        return len(self._l1)

    def summary(self) -> dict:
        return {
            **self.stats.to_dict(),
            "l1_size": self.l1_size(),
            "semantic_index": len(self._semantic_index),
        }


async def main():
    import sys

    cache = InferenceCache()
    await cache.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        model = "qwen/qwen3.5-9b"
        messages = [{"role": "user", "content": "What is Redis?"}]

        # Miss
        hit = await cache.get(model, messages)
        print(f"First get: {'hit' if hit else 'miss'}")

        # Set
        await cache.set(
            model,
            messages,
            "Redis is an in-memory store.",
            tokens_used=42,
            latency_ms=243.0,
        )

        # Hit
        hit = await cache.get(model, messages)
        print(
            f"Second get: {'hit' if hit else 'miss'} → '{hit.response if hit else ''}'"
        )

        print(f"\nStats: {json.dumps(cache.summary(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(cache.summary(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
