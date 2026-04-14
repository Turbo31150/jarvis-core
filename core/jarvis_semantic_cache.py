#!/usr/bin/env python3
"""
jarvis_semantic_cache — Semantic similarity cache for LLM responses
Vector-based lookup, cosine similarity dedup, TTL eviction, hit/miss tracking
"""

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.semantic_cache")

REDIS_PREFIX = "jarvis:scache:"


class CachePolicy(str, Enum):
    LRU = "lru"
    TTL = "ttl"
    HYBRID = "hybrid"  # TTL + LRU eviction


@dataclass
class CacheEntry:
    key: str
    prompt: str
    response: str
    embedding: list[float]
    model: str = ""
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    access_count: int = 0
    ttl_s: float = 3600.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.created_at) > self.ttl_s

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "prompt": self.prompt[:100],
            "model": self.model,
            "created_at": self.created_at,
            "last_accessed": self.last_accessed,
            "access_count": self.access_count,
            "ttl_remaining": round(
                max(0.0, self.ttl_s - (time.time() - self.created_at)), 1
            ),
        }


@dataclass
class CacheHit:
    entry: CacheEntry
    similarity: float
    hit: bool = True

    def to_dict(self) -> dict:
        return {
            "hit": self.hit,
            "similarity": round(self.similarity, 4),
            "model": self.entry.model,
            "response_preview": self.entry.response[:100],
            "age_s": round(time.time() - self.entry.created_at, 1),
        }


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _mock_embed(text: str) -> list[float]:
    """Deterministic mock embedding using char codes (384-dim)."""
    import hashlib

    seed = int(hashlib.md5(text.encode()).hexdigest(), 16)
    vec = []
    for i in range(384):
        seed = (seed * 1664525 + 1013904223) & 0xFFFFFFFF
        vec.append((seed / 0xFFFFFFFF - 0.5) * 2)
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


class SemanticCache:
    def __init__(
        self,
        similarity_threshold: float = 0.92,
        max_entries: int = 10_000,
        default_ttl_s: float = 3600.0,
        policy: CachePolicy = CachePolicy.HYBRID,
        embed_fn=None,
    ):
        self.redis: aioredis.Redis | None = None
        self._entries: dict[str, CacheEntry] = {}
        self._threshold = similarity_threshold
        self._max_entries = max_entries
        self._default_ttl = default_ttl_s
        self._policy = policy
        self._embed_fn = embed_fn or _mock_embed
        self._stats: dict[str, int] = {
            "gets": 0,
            "hits": 0,
            "misses": 0,
            "puts": 0,
            "evictions": 0,
            "expirations": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def _embed(self, text: str) -> list[float]:
        if asyncio.iscoroutinefunction(self._embed_fn):
            # sync fallback
            return _mock_embed(text)
        return self._embed_fn(text)

    async def _embed_async(self, text: str) -> list[float]:
        if asyncio.iscoroutinefunction(self._embed_fn):
            return await self._embed_fn(text)
        return self._embed_fn(text)

    def _evict(self):
        """Evict entries to stay under max_entries."""
        expired = [k for k, e in self._entries.items() if e.is_expired]
        for k in expired:
            del self._entries[k]
            self._stats["expirations"] += 1

        if len(self._entries) < self._max_entries:
            return

        # LRU eviction
        n_evict = max(1, len(self._entries) - self._max_entries + 100)
        lru_keys = sorted(self._entries, key=lambda k: self._entries[k].last_accessed)
        for k in lru_keys[:n_evict]:
            del self._entries[k]
            self._stats["evictions"] += 1

    async def get(
        self,
        prompt: str,
        model: str = "",
        threshold: float | None = None,
    ) -> CacheHit | None:
        self._stats["gets"] += 1
        thresh = threshold or self._threshold
        embedding = await self._embed_async(prompt)

        best_sim = 0.0
        best_entry: CacheEntry | None = None

        for entry in self._entries.values():
            if entry.is_expired:
                continue
            if model and entry.model and entry.model != model:
                continue
            sim = _cosine(embedding, entry.embedding)
            if sim > best_sim:
                best_sim = sim
                best_entry = entry

        if best_entry and best_sim >= thresh:
            best_entry.last_accessed = time.time()
            best_entry.access_count += 1
            self._stats["hits"] += 1
            log.debug(f"Cache HIT sim={best_sim:.3f} model={best_entry.model}")
            return CacheHit(entry=best_entry, similarity=best_sim, hit=True)

        self._stats["misses"] += 1
        return None

    async def put(
        self,
        prompt: str,
        response: str,
        model: str = "",
        ttl_s: float | None = None,
        metadata: dict | None = None,
    ) -> CacheEntry:
        import secrets

        self._evict()
        ttl = ttl_s or self._default_ttl
        embedding = await self._embed_async(prompt)
        key = secrets.token_hex(8)

        entry = CacheEntry(
            key=key,
            prompt=prompt,
            response=response,
            embedding=embedding,
            model=model,
            ttl_s=ttl,
            metadata=metadata or {},
        )
        self._entries[key] = entry
        self._stats["puts"] += 1

        if self.redis:
            asyncio.create_task(self._redis_store(entry))

        log.debug(f"Cache PUT key={key} model={model} ttl={ttl}s")
        return entry

    async def _redis_store(self, entry: CacheEntry):
        if not self.redis:
            return
        try:
            payload = {
                "key": entry.key,
                "prompt": entry.prompt,
                "response": entry.response,
                "model": entry.model,
                "metadata": entry.metadata,
            }
            await self.redis.setex(
                f"{REDIS_PREFIX}{entry.key}",
                int(entry.ttl_s),
                json.dumps(payload),
            )
        except Exception:
            pass

    def invalidate(self, key: str) -> bool:
        if key in self._entries:
            del self._entries[key]
            return True
        return False

    def invalidate_model(self, model: str) -> int:
        keys = [k for k, e in self._entries.items() if e.model == model]
        for k in keys:
            del self._entries[k]
        return len(keys)

    def purge_expired(self) -> int:
        expired = [k for k, e in self._entries.items() if e.is_expired]
        for k in expired:
            del self._entries[k]
        self._stats["expirations"] += len(expired)
        return len(expired)

    def top_accessed(self, limit: int = 10) -> list[dict]:
        return sorted(
            [e.to_dict() for e in self._entries.values() if not e.is_expired],
            key=lambda e: -e["access_count"],
        )[:limit]

    def stats(self) -> dict:
        active = sum(1 for e in self._entries.values() if not e.is_expired)
        return {
            **self._stats,
            "active_entries": active,
            "total_entries": len(self._entries),
            "hit_rate": round(self._stats["hits"] / max(self._stats["gets"], 1), 4),
            "threshold": self._threshold,
        }


def build_jarvis_semantic_cache(embed_fn=None) -> SemanticCache:
    return SemanticCache(
        similarity_threshold=0.92,
        max_entries=10_000,
        default_ttl_s=3600.0,
        policy=CachePolicy.HYBRID,
        embed_fn=embed_fn,
    )


async def main():
    import sys

    cache = build_jarvis_semantic_cache()
    await cache.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Semantic cache demo...\n")

        # Populate cache
        await cache.put(
            "What is the capital of France?",
            "The capital of France is Paris.",
            model="qwen3.5",
        )
        await cache.put(
            "List GPU models on M1", "M1 has RTX 3090 and RTX 3080.", model="qwen3.5"
        )
        await cache.put(
            "How do I restart an agent?",
            "Use: jarvis-agent restart <id>",
            model="deepseek-r1",
        )

        # Exact match
        hit = await cache.get("What is the capital of France?", model="qwen3.5")
        print(
            f"  Exact match: {'HIT' if hit else 'MISS'} sim={hit.similarity:.3f if hit else 0}"
        )

        # Semantic near-match
        hit2 = await cache.get("Capital city of France?")
        print(
            f"  Near match:  {'HIT' if hit2 else 'MISS'} sim={hit2.similarity:.3f if hit2 else 0}"
        )

        # Unrelated query
        hit3 = await cache.get("What is quantum entanglement?")
        print(f"  Unrelated:   {'HIT' if hit3 else 'MISS'}")

        # Model mismatch
        hit4 = await cache.get("What is the capital of France?", model="gemma3")
        print(f"  Model mismatch: {'HIT' if hit4 else 'MISS (expected)'}")

        print(f"\nTop accessed: {cache.top_accessed(3)}")
        print(f"\nStats: {json.dumps(cache.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(cache.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
