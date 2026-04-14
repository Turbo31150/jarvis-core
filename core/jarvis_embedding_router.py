#!/usr/bin/env python3
"""
jarvis_embedding_router — Route embedding requests to optimal model endpoints
Multi-model embedding with caching, batching, and fallback chains
"""

import asyncio
import hashlib
import json
import logging
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.embedding_router")

CACHE_FILE = Path("/home/turbo/IA/Core/jarvis/data/embedding_cache.jsonl")
REDIS_PREFIX = "jarvis:embed:"


@dataclass
class EmbeddingModel:
    model_id: str
    endpoint_url: str
    dimensions: int
    max_batch: int = 32
    max_tokens: int = 512
    priority: int = 5  # higher = preferred
    healthy: bool = True
    avg_latency_ms: float = 0.0
    total_requests: int = 0
    error_count: int = 0

    @property
    def error_rate(self) -> float:
        return self.error_count / max(self.total_requests, 1)

    def to_dict(self) -> dict:
        return {
            "model_id": self.model_id,
            "endpoint_url": self.endpoint_url,
            "dimensions": self.dimensions,
            "priority": self.priority,
            "healthy": self.healthy,
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "total_requests": self.total_requests,
            "error_rate": round(self.error_rate, 3),
        }


@dataclass
class EmbeddingResult:
    texts: list[str]
    embeddings: list[list[float]]
    model_id: str
    latency_ms: float
    cache_hits: int = 0
    from_cache: bool = False
    dimensions: int = 0

    def to_dict(self) -> dict:
        return {
            "count": len(self.embeddings),
            "model_id": self.model_id,
            "dimensions": self.dimensions,
            "latency_ms": round(self.latency_ms, 1),
            "cache_hits": self.cache_hits,
            "from_cache": self.from_cache,
        }


def _text_hash(text: str, model_id: str) -> str:
    return hashlib.sha256(f"{model_id}:{text}".encode()).hexdigest()[:20]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    ma = math.sqrt(sum(x * x for x in a))
    mb = math.sqrt(sum(x * x for x in b))
    return dot / max(ma * mb, 1e-9)


async def _call_embedding(
    endpoint_url: str,
    model_id: str,
    texts: list[str],
    timeout_s: float = 30.0,
) -> list[list[float]]:
    payload = {"model": model_id, "input": texts}
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=timeout_s)
    ) as sess:
        async with sess.post(f"{endpoint_url}/v1/embeddings", json=payload) as r:
            if r.status != 200:
                text = await r.text()
                raise RuntimeError(f"HTTP {r.status}: {text[:100]}")
            data = await r.json(content_type=None)
            items = sorted(data.get("data", []), key=lambda x: x.get("index", 0))
            return [item["embedding"] for item in items]


class EmbeddingRouter:
    def __init__(
        self,
        cache_size: int = 5000,
        cache_ttl_s: float = 3600.0,
        persist: bool = True,
    ):
        self.redis: aioredis.Redis | None = None
        self._models: list[EmbeddingModel] = []
        self._cache: dict[str, tuple[list[float], float]] = {}  # hash → (embedding, ts)
        self._cache_size = cache_size
        self._cache_ttl = cache_ttl_s
        self._persist = persist
        self._stats: dict[str, Any] = {
            "requests": 0,
            "texts_embedded": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "errors": 0,
            "fallbacks": 0,
        }
        if persist:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            self._load_cache()

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def _load_cache(self):
        if not CACHE_FILE.exists():
            return
        try:
            loaded = 0
            for line in CACHE_FILE.read_text().splitlines():
                if not line.strip():
                    continue
                d = json.loads(line)
                key = d["key"]
                emb = d["embedding"]
                ts = d.get("ts", time.time())
                if time.time() - ts < self._cache_ttl:
                    self._cache[key] = (emb, ts)
                    loaded += 1
                if loaded >= self._cache_size:
                    break
            log.info(f"Embedding cache loaded: {loaded} entries")
        except Exception as e:
            log.warning(f"Embedding cache load error: {e}")

    def add_model(self, model: EmbeddingModel):
        self._models.append(model)
        self._models.sort(key=lambda m: -m.priority)

    def _best_model(self) -> EmbeddingModel | None:
        healthy = [m for m in self._models if m.healthy]
        if not healthy:
            return None
        return min(
            healthy,
            key=lambda m: m.error_rate * 10 + m.avg_latency_ms / 1000 - m.priority,
        )

    def _cache_get(self, key: str) -> list[float] | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        emb, ts = entry
        if time.time() - ts > self._cache_ttl:
            del self._cache[key]
            return None
        return emb

    def _cache_set(self, key: str, embedding: list[float]):
        if len(self._cache) >= self._cache_size:
            # Evict oldest
            oldest = min(self._cache, key=lambda k: self._cache[k][1])
            del self._cache[oldest]
        self._cache[key] = (embedding, time.time())
        if self._persist:
            try:
                with open(CACHE_FILE, "a") as f:
                    f.write(
                        json.dumps(
                            {"key": key, "embedding": embedding, "ts": time.time()}
                        )
                        + "\n"
                    )
            except Exception:
                pass

    async def embed(
        self,
        texts: str | list[str],
        model_id: str | None = None,
        use_cache: bool = True,
    ) -> EmbeddingResult:
        if isinstance(texts, str):
            texts = [texts]

        self._stats["requests"] += 1
        t0 = time.time()

        # Select model
        if model_id:
            model = next((m for m in self._models if m.model_id == model_id), None)
        else:
            model = self._best_model()

        if not model:
            raise RuntimeError("No healthy embedding model available")

        # Check cache
        results: list[list[float] | None] = [None] * len(texts)
        uncached_indices: list[int] = []
        cache_hits = 0

        if use_cache:
            for i, text in enumerate(texts):
                key = _text_hash(text, model.model_id)
                cached = self._cache_get(key)
                if cached is not None:
                    results[i] = cached
                    cache_hits += 1
                    self._stats["cache_hits"] += 1
                else:
                    uncached_indices.append(i)
                    self._stats["cache_misses"] += 1
        else:
            uncached_indices = list(range(len(texts)))

        # Batch embed uncached texts
        if uncached_indices:
            uncached_texts = [texts[i] for i in uncached_indices]

            # Chunk into batches
            all_embeddings: list[list[float]] = []
            for chunk_start in range(0, len(uncached_texts), model.max_batch):
                chunk = uncached_texts[chunk_start : chunk_start + model.max_batch]
                try:
                    embeddings = await _call_embedding(
                        model.endpoint_url, model.model_id, chunk
                    )
                    all_embeddings.extend(embeddings)
                    model.total_requests += 1
                except Exception as e:
                    model.error_count += 1
                    model.total_requests += 1
                    self._stats["errors"] += 1
                    log.warning(f"Embedding error on {model.model_id}: {e}")

                    # Try fallback
                    fallback = next(
                        (
                            m
                            for m in self._models
                            if m.healthy and m.model_id != model.model_id
                        ),
                        None,
                    )
                    if fallback:
                        self._stats["fallbacks"] += 1
                        embeddings = await _call_embedding(
                            fallback.endpoint_url, fallback.model_id, chunk
                        )
                        all_embeddings.extend(embeddings)
                    else:
                        raise

            for idx, emb in zip(uncached_indices, all_embeddings):
                results[idx] = emb
                if use_cache:
                    key = _text_hash(texts[idx], model.model_id)
                    self._cache_set(key, emb)

        final_embeddings = [e for e in results if e is not None]
        latency_ms = (time.time() - t0) * 1000

        # Update model stats
        model.avg_latency_ms = (
            model.avg_latency_ms * 0.9 + latency_ms * 0.1
            if model.avg_latency_ms > 0
            else latency_ms
        )

        self._stats["texts_embedded"] += len(final_embeddings)
        dims = len(final_embeddings[0]) if final_embeddings else 0

        return EmbeddingResult(
            texts=texts,
            embeddings=final_embeddings,
            model_id=model.model_id,
            latency_ms=latency_ms,
            cache_hits=cache_hits,
            from_cache=(cache_hits == len(texts)),
            dimensions=dims,
        )

    async def similarity(self, text_a: str, text_b: str) -> float:
        result = await self.embed([text_a, text_b])
        if len(result.embeddings) < 2:
            return 0.0
        return _cosine(result.embeddings[0], result.embeddings[1])

    def models(self) -> list[dict]:
        return [m.to_dict() for m in self._models]

    def stats(self) -> dict:
        total = self._stats["cache_hits"] + self._stats["cache_misses"]
        hit_rate = self._stats["cache_hits"] / max(total, 1)
        return {
            **self._stats,
            "cache_hit_rate": round(hit_rate, 3),
            "cache_size": len(self._cache),
            "models": len(self._models),
        }


def build_jarvis_embedding_router() -> EmbeddingRouter:
    router = EmbeddingRouter(cache_size=5000, cache_ttl_s=3600.0, persist=True)

    router.add_model(
        EmbeddingModel(
            model_id="nomic-embed-text",
            endpoint_url="http://192.168.1.26:1234",
            dimensions=768,
            max_batch=32,
            priority=9,
        )
    )
    router.add_model(
        EmbeddingModel(
            model_id="text-embedding-nomic-embed-text-v1.5",
            endpoint_url="http://192.168.1.85:1234",
            dimensions=768,
            max_batch=16,
            priority=7,
        )
    )

    return router


async def main():
    import sys

    router = build_jarvis_embedding_router()
    await router.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Models:")
        for m in router.models():
            print(
                f"  {m['model_id']:<45} dims={m['dimensions']} priority={m['priority']}"
            )

        texts = [
            "JARVIS cluster management system",
            "Redis pub/sub distributed messaging",
            "GPU memory optimization techniques",
        ]

        print(f"\nEmbedding {len(texts)} texts...")
        try:
            result = await router.embed(texts)
            print(f"Result: {result.to_dict()}")

            # Similarity
            sim = await router.similarity(texts[0], texts[1])
            print(f"\nSimilarity '{texts[0][:30]}' vs '{texts[1][:30]}': {sim:.4f}")

            # Cache hit on re-request
            result2 = await router.embed(texts[:2])
            print(
                f"Second request (cached): hits={result2.cache_hits}/{len(result2.texts)}"
            )
        except Exception as e:
            print(f"Error (no model endpoint): {e}")

        print(f"\nStats: {json.dumps(router.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(router.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
