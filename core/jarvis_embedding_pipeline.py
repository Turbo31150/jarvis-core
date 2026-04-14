#!/usr/bin/env python3
"""
jarvis_embedding_pipeline — Text embedding pipeline with caching and batch processing
Generates embeddings via local models, stores in Redis/file for reuse
"""

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.embedding_pipeline")

REDIS_PREFIX = "jarvis:embed:"
EMBED_CACHE_FILE = Path("/home/turbo/IA/Core/jarvis/data/embeddings_cache.json")
EMBED_TTL = 86400 * 7  # 7 days

# Embedding backends
BACKENDS = {
    "nomic": {
        "url": "http://192.168.1.26:1234",  # M2 has nomic-embed loaded
        "model": "text-embedding-nomic-embed-text-v1.5",
        "dim": 768,
    },
    "local_fallback": {
        "url": "http://127.0.0.1:11434",  # OL1 Ollama
        "model": "nomic-embed-text",
        "dim": 768,
    },
}

DEFAULT_BACKEND = "nomic"


@dataclass
class EmbeddingResult:
    text: str
    embedding: list[float]
    model: str
    backend: str
    from_cache: bool = False
    latency_ms: float = 0.0

    @property
    def dim(self) -> int:
        return len(self.embedding)

    def cosine_similarity(self, other: "EmbeddingResult") -> float:
        a, b = self.embedding, other.embedding
        if len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        mag_a = sum(x * x for x in a) ** 0.5
        mag_b = sum(x * x for x in b) ** 0.5
        if mag_a == 0 or mag_b == 0:
            return 0.0
        return round(dot / (mag_a * mag_b), 6)


def _text_hash(text: str, model: str) -> str:
    return hashlib.sha256(f"{model}::{text}".encode()).hexdigest()[:16]


class EmbeddingPipeline:
    def __init__(self, backend: str = DEFAULT_BACKEND):
        self.redis: aioredis.Redis | None = None
        self.backend_name = backend
        self.backend = BACKENDS[backend]
        self._local_cache: dict[str, list[float]] = {}
        self._stats = {"hits": 0, "misses": 0, "errors": 0}
        self._load_file_cache()

    def _load_file_cache(self):
        if EMBED_CACHE_FILE.exists():
            try:
                data = json.loads(EMBED_CACHE_FILE.read_text())
                self._local_cache = data
                log.debug(f"Loaded {len(data)} embeddings from file cache")
            except Exception:
                pass

    def _save_file_cache(self):
        EMBED_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Keep last 10000 entries
        if len(self._local_cache) > 10000:
            keys = list(self._local_cache.keys())
            for k in keys[:-10000]:
                del self._local_cache[k]
        EMBED_CACHE_FILE.write_text(json.dumps(self._local_cache))

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    async def _get_cached(self, text_hash: str) -> list[float] | None:
        if text_hash in self._local_cache:
            return self._local_cache[text_hash]
        if self.redis:
            raw = await self.redis.get(f"{REDIS_PREFIX}{text_hash}")
            if raw:
                embedding = json.loads(raw)
                self._local_cache[text_hash] = embedding
                return embedding
        return None

    async def _set_cached(self, text_hash: str, embedding: list[float]):
        self._local_cache[text_hash] = embedding
        if self.redis:
            await self.redis.set(
                f"{REDIS_PREFIX}{text_hash}",
                json.dumps(embedding),
                ex=EMBED_TTL,
            )

    async def _embed_openai_format(self, text: str) -> list[float]:
        """Call OpenAI-compatible /v1/embeddings endpoint."""
        t0 = time.time()
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        ) as sess:
            async with sess.post(
                f"{self.backend['url']}/v1/embeddings",
                json={"model": self.backend["model"], "input": text},
            ) as r:
                data = await r.json()

        elapsed_ms = (time.time() - t0) * 1000
        embedding = data["data"][0]["embedding"]
        log.debug(f"Embedded {len(text)} chars in {elapsed_ms:.0f}ms")
        return embedding

    async def _embed_ollama(self, text: str) -> list[float]:
        """Call Ollama /api/embeddings endpoint."""
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        ) as sess:
            async with sess.post(
                f"{self.backend['url']}/api/embeddings",
                json={"model": self.backend["model"], "prompt": text},
            ) as r:
                data = await r.json()
        return data["embedding"]

    async def embed(self, text: str) -> EmbeddingResult:
        text = text.strip()
        h = _text_hash(text, self.backend["model"])
        t0 = time.time()

        cached = await self._get_cached(h)
        if cached:
            self._stats["hits"] += 1
            return EmbeddingResult(
                text=text[:100],
                embedding=cached,
                model=self.backend["model"],
                backend=self.backend_name,
                from_cache=True,
            )

        self._stats["misses"] += 1
        try:
            if "11434" in self.backend["url"]:
                embedding = await self._embed_ollama(text)
            else:
                embedding = await self._embed_openai_format(text)

            await self._set_cached(h, embedding)
            self._save_file_cache()

            return EmbeddingResult(
                text=text[:100],
                embedding=embedding,
                model=self.backend["model"],
                backend=self.backend_name,
                latency_ms=round((time.time() - t0) * 1000, 1),
            )
        except Exception as e:
            self._stats["errors"] += 1
            log.error(f"Embedding error: {e}")
            # Return zero vector as fallback
            return EmbeddingResult(
                text=text[:100],
                embedding=[0.0] * self.backend["dim"],
                model=self.backend["model"],
                backend="fallback",
                latency_ms=round((time.time() - t0) * 1000, 1),
            )

    async def embed_batch(
        self, texts: list[str], concurrency: int = 5
    ) -> list[EmbeddingResult]:
        sem = asyncio.Semaphore(concurrency)

        async def embed_one(text: str) -> EmbeddingResult:
            async with sem:
                return await self.embed(text)

        return await asyncio.gather(*[embed_one(t) for t in texts])

    async def similarity(self, text_a: str, text_b: str) -> float:
        results = await self.embed_batch([text_a, text_b])
        return results[0].cosine_similarity(results[1])

    async def find_most_similar(
        self, query: str, candidates: list[str], top_k: int = 3
    ) -> list[tuple[str, float]]:
        all_texts = [query] + candidates
        embeddings = await self.embed_batch(all_texts)
        query_emb = embeddings[0]
        scored = [
            (candidates[i], query_emb.cosine_similarity(embeddings[i + 1]))
            for i in range(len(candidates))
        ]
        return sorted(scored, key=lambda x: -x[1])[:top_k]

    def stats(self) -> dict:
        total = self._stats["hits"] + self._stats["misses"]
        return {
            **self._stats,
            "hit_rate_pct": round(self._stats["hits"] / total * 100, 1)
            if total > 0
            else 0,
            "cached_entries": len(self._local_cache),
            "backend": self.backend_name,
            "model": self.backend["model"],
        }


# ── CLI ───────────────────────────────────────────────────────────────────────


async def main():
    import sys

    pipeline = EmbeddingPipeline()
    await pipeline.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"

    if cmd == "embed" and len(sys.argv) > 2:
        text = sys.argv[2]
        result = await pipeline.embed(text)
        print(f"Text: {result.text[:60]}")
        print(f"Model: {result.model} | Backend: {result.backend}")
        print(
            f"Dim: {result.dim} | Cached: {result.from_cache} | {result.latency_ms:.0f}ms"
        )
        print(f"First 5 values: {result.embedding[:5]}")

    elif cmd == "similarity" and len(sys.argv) > 3:
        score = await pipeline.similarity(sys.argv[2], sys.argv[3])
        print(f"Similarity: {score:.4f}")

    elif cmd == "find" and len(sys.argv) > 3:
        query = sys.argv[2]
        candidates = sys.argv[3:]
        results = await pipeline.find_most_similar(query, candidates)
        print(f"Query: '{query}'")
        for text, score in results:
            print(f"  {score:.4f} — {text[:80]}")

    elif cmd == "stats":
        print(json.dumps(pipeline.stats(), indent=2))

    elif cmd == "batch" and len(sys.argv) > 2:
        texts = sys.argv[2:]
        results = await pipeline.embed_batch(texts)
        for r in results:
            print(
                f"  {'[cache]' if r.from_cache else '[new]  '} {r.text[:60]} ({r.dim}d)"
            )


if __name__ == "__main__":
    asyncio.run(main())
