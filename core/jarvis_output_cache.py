#!/usr/bin/env python3
"""
jarvis_output_cache — Semantic and exact caching for LLM outputs
Exact match on prompt hash, semantic match via embedding similarity, TTL management
"""

import asyncio
import hashlib
import json
import logging
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.output_cache")

REDIS_PREFIX = "jarvis:outcache:"
CACHE_FILE = Path("/home/turbo/IA/Core/jarvis/data/output_cache.jsonl")


class CacheHitType(str, Enum):
    EXACT = "exact"
    SEMANTIC = "semantic"
    NONE = "none"


@dataclass
class CacheEntry:
    cache_key: str
    prompt_hash: str
    model: str
    messages_repr: str  # compact repr for logging
    response: str
    embedding: list[float] = field(default_factory=list)
    hit_count: int = 0
    created_at: float = field(default_factory=time.time)
    last_hit: float = 0.0
    ttl_s: float = 3600.0

    @property
    def expired(self) -> bool:
        return self.ttl_s > 0 and (time.time() - self.created_at) > self.ttl_s

    def to_dict(self, include_embedding: bool = False) -> dict:
        d = {
            "cache_key": self.cache_key,
            "prompt_hash": self.prompt_hash,
            "model": self.model,
            "messages_repr": self.messages_repr[:100],
            "response": self.response[:200],
            "hit_count": self.hit_count,
            "created_at": self.created_at,
            "ttl_s": self.ttl_s,
        }
        if include_embedding and self.embedding:
            d["embedding"] = self.embedding
        return d


@dataclass
class CacheResult:
    hit_type: CacheHitType
    entry: CacheEntry | None
    similarity: float = 1.0

    @property
    def hit(self) -> bool:
        return self.hit_type != CacheHitType.NONE

    def to_dict(self) -> dict:
        return {
            "hit": self.hit,
            "hit_type": self.hit_type.value,
            "similarity": round(self.similarity, 4),
            "response": self.entry.response if self.entry else None,
        }


def _messages_hash(messages: list[dict], model: str) -> str:
    canonical = json.dumps({"model": model, "messages": messages}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:24]


def _messages_repr(messages: list[dict]) -> str:
    return " | ".join(
        f"{m.get('role', '?')}:{str(m.get('content', ''))[:40]}" for m in messages
    )


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    ma = math.sqrt(sum(x * x for x in a))
    mb = math.sqrt(sum(x * x for x in b))
    return dot / max(ma * mb, 1e-9)


class OutputCache:
    def __init__(
        self,
        max_size: int = 2000,
        default_ttl_s: float = 3600.0,
        semantic_threshold: float = 0.92,
        persist: bool = True,
    ):
        self.redis: aioredis.Redis | None = None
        self._entries: dict[str, CacheEntry] = {}  # prompt_hash → entry
        self._max_size = max_size
        self._default_ttl = default_ttl_s
        self._semantic_threshold = semantic_threshold
        self._persist = persist
        self._stats: dict[str, int] = {
            "exact_hits": 0,
            "semantic_hits": 0,
            "misses": 0,
            "sets": 0,
            "evictions": 0,
            "expirations": 0,
        }
        if persist:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            self._load()

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def _load(self):
        if not CACHE_FILE.exists():
            return
        try:
            for line in CACHE_FILE.read_text().splitlines():
                if not line.strip():
                    continue
                d = json.loads(line)
                entry = CacheEntry(
                    cache_key=d["cache_key"],
                    prompt_hash=d["prompt_hash"],
                    model=d.get("model", ""),
                    messages_repr=d.get("messages_repr", ""),
                    response=d.get("response", ""),
                    embedding=d.get("embedding", []),
                    hit_count=d.get("hit_count", 0),
                    created_at=d.get("created_at", time.time()),
                    ttl_s=d.get("ttl_s", self._default_ttl),
                )
                if not entry.expired:
                    self._entries[entry.prompt_hash] = entry
        except Exception as e:
            log.warning(f"Output cache load error: {e}")

    def get(
        self,
        messages: list[dict],
        model: str,
        query_embedding: list[float] | None = None,
    ) -> CacheResult:
        # Exact match first
        ph = _messages_hash(messages, model)
        entry = self._entries.get(ph)
        if entry:
            if entry.expired:
                del self._entries[ph]
                self._stats["expirations"] += 1
            else:
                entry.hit_count += 1
                entry.last_hit = time.time()
                self._stats["exact_hits"] += 1
                return CacheResult(CacheHitType.EXACT, entry, 1.0)

        # Redis exact fallback
        if self.redis:
            result = self._redis_get_sync(ph)
            if result:
                return result

        # Semantic match
        if query_embedding and len(query_embedding) > 0:
            best_sim = 0.0
            best_entry = None
            for e in self._entries.values():
                if e.model != model or e.expired or not e.embedding:
                    continue
                sim = _cosine(query_embedding, e.embedding)
                if sim > best_sim:
                    best_sim = sim
                    best_entry = e
            if best_entry and best_sim >= self._semantic_threshold:
                best_entry.hit_count += 1
                best_entry.last_hit = time.time()
                self._stats["semantic_hits"] += 1
                return CacheResult(CacheHitType.SEMANTIC, best_entry, best_sim)

        self._stats["misses"] += 1
        return CacheResult(CacheHitType.NONE, None, 0.0)

    def _redis_get_sync(self, prompt_hash: str) -> CacheResult | None:
        """Placeholder — real implementation would be async."""
        return None

    def set(
        self,
        messages: list[dict],
        model: str,
        response: str,
        embedding: list[float] | None = None,
        ttl_s: float | None = None,
    ) -> CacheEntry:
        ph = _messages_hash(messages, model)
        ttl = ttl_s if ttl_s is not None else self._default_ttl

        entry = CacheEntry(
            cache_key=ph,
            prompt_hash=ph,
            model=model,
            messages_repr=_messages_repr(messages),
            response=response,
            embedding=embedding or [],
            ttl_s=ttl,
        )

        # Evict LRU if at capacity
        if len(self._entries) >= self._max_size:
            oldest_key = min(
                self._entries,
                key=lambda k: self._entries[k].last_hit or self._entries[k].created_at,
            )
            del self._entries[oldest_key]
            self._stats["evictions"] += 1

        self._entries[ph] = entry
        self._stats["sets"] += 1

        if self._persist:
            try:
                with open(CACHE_FILE, "a") as f:
                    f.write(json.dumps(entry.to_dict(include_embedding=True)) + "\n")
            except Exception:
                pass

        if self.redis:
            asyncio.create_task(self._redis_set(ph, entry, ttl))

        return entry

    async def _redis_set(self, key: str, entry: CacheEntry, ttl_s: float):
        try:
            redis_key = f"{REDIS_PREFIX}{key}"
            payload = json.dumps(entry.to_dict())
            if ttl_s > 0:
                await self.redis.setex(redis_key, int(ttl_s), payload)
            else:
                await self.redis.set(redis_key, payload)
        except Exception as e:
            log.warning(f"Redis cache set error: {e}")

    def invalidate(self, model: str | None = None):
        if model:
            to_remove = [k for k, e in self._entries.items() if e.model == model]
        else:
            to_remove = list(self._entries.keys())
        for k in to_remove:
            del self._entries[k]
        return len(to_remove)

    def purge_expired(self) -> int:
        expired = [k for k, e in self._entries.items() if e.expired]
        for k in expired:
            del self._entries[k]
        self._stats["expirations"] += len(expired)
        return len(expired)

    def top_entries(self, n: int = 10) -> list[dict]:
        sorted_entries = sorted(self._entries.values(), key=lambda e: -e.hit_count)
        return [e.to_dict() for e in sorted_entries[:n]]

    def stats(self) -> dict:
        total = (
            self._stats["exact_hits"]
            + self._stats["semantic_hits"]
            + self._stats["misses"]
        )
        hit_rate = (self._stats["exact_hits"] + self._stats["semantic_hits"]) / max(
            total, 1
        )
        return {
            **self._stats,
            "total_entries": len(self._entries),
            "hit_rate": round(hit_rate, 3),
            "semantic_threshold": self._semantic_threshold,
        }


def build_jarvis_output_cache() -> OutputCache:
    return OutputCache(
        max_size=2000,
        default_ttl_s=1800.0,
        semantic_threshold=0.93,
        persist=True,
    )


async def main():
    import sys

    cache = build_jarvis_output_cache()
    await cache.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        model = "qwen3.5-9b"
        msgs1 = [{"role": "user", "content": "What is the capital of France?"}]
        msgs2 = [
            {"role": "user", "content": "What is the capital of France?"}
        ]  # duplicate
        msgs3 = [
            {"role": "user", "content": "What is the main city of France?"}
        ]  # similar

        # Miss
        r = cache.get(msgs1, model)
        print(f"First get: hit={r.hit} type={r.hit_type.value}")

        # Set
        cache.set(msgs1, model, "The capital of France is Paris.", ttl_s=300)

        # Exact hit
        r2 = cache.get(msgs2, model)
        print(f"Exact hit: hit={r2.hit} type={r2.hit_type.value}")

        # Semantic hit (simulated embedding)
        fake_emb = [0.1] * 64
        cache._entries[list(cache._entries.keys())[0]].embedding = fake_emb
        r3 = cache.get(msgs3, model, query_embedding=fake_emb)
        print(
            f"Semantic hit: hit={r3.hit} type={r3.hit_type.value} sim={r3.similarity:.4f}"
        )

        print("\nTop entries:")
        for e in cache.top_entries(5):
            print(f"  {e['prompt_hash'][:12]} hits={e['hit_count']} model={e['model']}")

        print(f"\nStats: {json.dumps(cache.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(cache.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
