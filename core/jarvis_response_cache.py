#!/usr/bin/env python3
"""
jarvis_response_cache — Semantic response caching with similarity-based lookup
Caches LLM responses by prompt hash and embedding similarity, reduces redundant inference
"""

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.response_cache")

CACHE_FILE = Path("/home/turbo/IA/Core/jarvis/data/response_cache.json")
REDIS_PREFIX = "jarvis:rcache:"
LM_URL = "http://127.0.0.1:1234"
EMBED_URL = "http://192.168.1.26:1234"  # M2 has nomic-embed


@dataclass
class CachedResponse:
    cache_id: str
    prompt_hash: str
    prompt: str
    response: str
    model: str
    embedding: list[float]
    hits: int = 0
    created_at: float = field(default_factory=time.time)
    ttl_s: float = 3600.0

    @property
    def expired(self) -> bool:
        return time.time() - self.created_at > self.ttl_s

    def to_dict(self) -> dict:
        return {
            "cache_id": self.cache_id,
            "prompt_hash": self.prompt_hash,
            "prompt": self.prompt[:200],
            "response": self.response[:500],
            "model": self.model,
            "embedding": self.embedding[:10],  # truncate for storage
            "hits": self.hits,
            "created_at": self.created_at,
            "ttl_s": self.ttl_s,
        }


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


def _prompt_hash(prompt: str, model: str) -> str:
    return hashlib.sha256(f"{model}:{prompt}".encode()).hexdigest()[:16]


class ResponseCache:
    def __init__(
        self,
        exact_ttl_s: float = 3600.0,
        semantic_threshold: float = 0.92,
        max_entries: int = 5000,
    ):
        self.exact_ttl_s = exact_ttl_s
        self.semantic_threshold = semantic_threshold
        self.max_entries = max_entries
        self.redis: aioredis.Redis | None = None
        self._store: dict[str, CachedResponse] = {}
        self._hits = 0
        self._misses = 0
        self._semantic_hits = 0
        self._load()

    def _load(self):
        if CACHE_FILE.exists():
            try:
                data = json.loads(CACHE_FILE.read_text())
                for d in data.get("entries", []):
                    entry = CachedResponse(
                        cache_id=d["cache_id"],
                        prompt_hash=d["prompt_hash"],
                        prompt=d["prompt"],
                        response=d["response"],
                        model=d["model"],
                        embedding=[],  # don't load truncated embeddings
                        hits=d.get("hits", 0),
                        created_at=d["created_at"],
                        ttl_s=d.get("ttl_s", self.exact_ttl_s),
                    )
                    if not entry.expired:
                        self._store[entry.prompt_hash] = entry
                log.debug(f"Loaded {len(self._store)} cache entries")
            except Exception as e:
                log.warning(f"Cache load error: {e}")

    def _save(self):
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        valid = [e for e in self._store.values() if not e.expired]
        CACHE_FILE.write_text(
            json.dumps(
                {"entries": [e.to_dict() for e in valid[-self.max_entries :]]},
                indent=2,
            )
        )

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    async def _get_embedding(self, text: str) -> list[float]:
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            ) as sess:
                async with sess.post(
                    f"{EMBED_URL}/v1/embeddings",
                    json={
                        "model": "nomic-ai/nomic-embed-text-v1.5-GGUF",
                        "input": text[:500],
                    },
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        return data["data"][0]["embedding"]
        except Exception as e:
            log.debug(f"Embedding error: {e}")
        return []

    async def get(
        self,
        prompt: str,
        model: str,
        use_semantic: bool = True,
    ) -> tuple[str | None, str]:
        """
        Returns (response, cache_type) where cache_type is 'exact', 'semantic', or 'miss'.
        """
        key = _prompt_hash(prompt, model)

        # 1. Exact match
        entry = self._store.get(key)
        if entry and not entry.expired:
            entry.hits += 1
            self._hits += 1
            if self.redis:
                await self.redis.hincrby(f"{REDIS_PREFIX}stats", "hits", 1)
            log.debug(f"Cache hit (exact): {key}")
            return entry.response, "exact"

        # 2. Redis exact match
        if self.redis:
            cached = await self.redis.get(f"{REDIS_PREFIX}{key}")
            if cached:
                self._hits += 1
                return json.loads(cached)["response"], "exact"

        # 3. Semantic similarity
        if use_semantic and len(self._store) > 0:
            query_emb = await self._get_embedding(prompt)
            if query_emb:
                best_score = 0.0
                best_entry = None
                for e in self._store.values():
                    if e.expired or e.model != model or not e.embedding:
                        continue
                    sim = _cosine_similarity(query_emb, e.embedding)
                    if sim > best_score:
                        best_score = sim
                        best_entry = e

                if best_entry and best_score >= self.semantic_threshold:
                    best_entry.hits += 1
                    self._hits += 1
                    self._semantic_hits += 1
                    log.info(f"Cache hit (semantic sim={best_score:.3f}): {key}")
                    if self.redis:
                        await self.redis.hincrby(
                            f"{REDIS_PREFIX}stats", "semantic_hits", 1
                        )
                    return best_entry.response, "semantic"

        self._misses += 1
        if self.redis:
            await self.redis.hincrby(f"{REDIS_PREFIX}stats", "misses", 1)
        return None, "miss"

    async def put(
        self,
        prompt: str,
        response: str,
        model: str,
        ttl_s: float | None = None,
        get_embedding: bool = True,
    ) -> CachedResponse:
        import uuid

        key = _prompt_hash(prompt, model)
        embedding = []
        if get_embedding:
            embedding = await self._get_embedding(prompt)

        entry = CachedResponse(
            cache_id=str(uuid.uuid4())[:8],
            prompt_hash=key,
            prompt=prompt,
            response=response,
            model=model,
            embedding=embedding,
            ttl_s=ttl_s or self.exact_ttl_s,
        )

        # Evict if over limit
        if len(self._store) >= self.max_entries:
            oldest = min(self._store.values(), key=lambda e: e.created_at)
            del self._store[oldest.prompt_hash]

        self._store[key] = entry

        if self.redis:
            await self.redis.setex(
                f"{REDIS_PREFIX}{key}",
                int(entry.ttl_s),
                json.dumps({"response": response, "model": model}),
            )

        self._save()
        return entry

    def stats(self) -> dict:
        total = self._hits + self._misses
        valid = [e for e in self._store.values() if not e.expired]
        return {
            "entries": len(valid),
            "total_requests": total,
            "hits": self._hits,
            "misses": self._misses,
            "semantic_hits": self._semantic_hits,
            "hit_rate": round(self._hits / max(total, 1) * 100, 1),
            "semantic_rate": round(self._semantic_hits / max(self._hits, 1) * 100, 1),
        }

    async def invalidate(self, model: str | None = None):
        if model:
            keys = [k for k, e in self._store.items() if e.model == model]
        else:
            keys = list(self._store.keys())
        for k in keys:
            del self._store[k]
            if self.redis:
                await self.redis.delete(f"{REDIS_PREFIX}{k}")
        self._save()
        log.info(f"Invalidated {len(keys)} cache entries (model={model or 'all'})")
        return len(keys)


async def main():
    import sys

    cache = ResponseCache(semantic_threshold=0.90)
    await cache.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        model = "qwen/qwen3.5-9b"
        # Store a response
        await cache.put(
            "What is Redis?", "Redis is an in-memory key-value store.", model
        )
        await cache.put(
            "Explain Python async.",
            "Python async uses coroutines and event loops.",
            model,
        )

        # Exact hit
        resp, ctype = await cache.get("What is Redis?", model, use_semantic=False)
        print(f"Exact hit ({ctype}): {resp}")

        # Semantic hit
        resp, ctype = await cache.get("Can you explain what Redis is?", model)
        print(f"Semantic hit ({ctype}): {resp}")

        s = cache.stats()
        print(
            f"\nStats: hit_rate={s['hit_rate']}% semantic_rate={s['semantic_rate']}% entries={s['entries']}"
        )

    elif cmd == "stats":
        print(json.dumps(cache.stats(), indent=2))

    elif cmd == "invalidate":
        model = sys.argv[2] if len(sys.argv) > 2 else None
        count = await cache.invalidate(model)
        print(f"Invalidated {count} entries")


if __name__ == "__main__":
    asyncio.run(main())
