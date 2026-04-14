#!/usr/bin/env python3
"""
jarvis_batch_embedder — High-throughput batch embedding pipeline
Chunks large text corpora, batches embedding requests, stores results
"""

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.batch_embedder")

REDIS_PREFIX = "jarvis:embed:"
CACHE_FILE = Path("/home/turbo/IA/Core/jarvis/data/embeddings_cache.jsonl")

DEFAULT_EMBED_URL = "http://192.168.1.26:1234"
DEFAULT_MODEL = "nomic-embed-text"


@dataclass
class EmbedRequest:
    text: str
    doc_id: str = ""
    chunk_index: int = 0
    metadata: dict = field(default_factory=dict)


@dataclass
class EmbedResult:
    doc_id: str
    chunk_index: int
    text: str
    embedding: list[float]
    model: str
    dim: int
    ts: float = field(default_factory=time.time)

    def to_dict(self, include_vector: bool = True) -> dict:
        d = {
            "doc_id": self.doc_id,
            "chunk_index": self.chunk_index,
            "text_preview": self.text[:80],
            "model": self.model,
            "dim": self.dim,
            "ts": self.ts,
        }
        if include_vector:
            d["embedding"] = self.embedding
        return d


def _chunk_text(text: str, chunk_size: int = 512, overlap: int = 64) -> list[str]:
    """Split text into overlapping word-based chunks."""
    words = text.split()
    if not words:
        return []
    chunks = []
    step = max(1, chunk_size - overlap)
    for i in range(0, len(words), step):
        chunk = " ".join(words[i : i + chunk_size])
        if chunk:
            chunks.append(chunk)
    return chunks


def _cosine_sim(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    ma = math.sqrt(sum(x * x for x in a))
    mb = math.sqrt(sum(x * x for x in b))
    return dot / max(ma * mb, 1e-9)


class BatchEmbedder:
    def __init__(
        self,
        embed_url: str = DEFAULT_EMBED_URL,
        model: str = DEFAULT_MODEL,
        batch_size: int = 32,
        concurrency: int = 4,
        persist: bool = True,
    ):
        self.redis: aioredis.Redis | None = None
        self._url = embed_url
        self._model = model
        self._batch_size = batch_size
        self._concurrency = concurrency
        self._persist = persist
        self._results: list[EmbedResult] = []
        self._cache: dict[str, list[float]] = {}  # text_hash → embedding
        self._stats: dict[str, int | float] = {
            "texts_embedded": 0,
            "cache_hits": 0,
            "batches_sent": 0,
            "errors": 0,
            "total_ms": 0.0,
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
            for line in CACHE_FILE.read_text().splitlines():
                if not line.strip():
                    continue
                d = json.loads(line)
                if "embedding" in d and d["embedding"]:
                    key = d.get("text_preview", "")[:80]
                    if key:
                        self._cache[key] = d["embedding"]
        except Exception as e:
            log.warning(f"Cache load error: {e}")

    async def _embed_batch(
        self, texts: list[str], session: aiohttp.ClientSession
    ) -> list[list[float]]:
        t0 = time.time()
        try:
            async with session.post(
                f"{self._url}/v1/embeddings",
                json={"model": self._model, "input": texts},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                self._stats["total_ms"] = (
                    float(self._stats["total_ms"]) + (time.time() - t0) * 1000
                )
                if r.status == 200:
                    data = await r.json(content_type=None)
                    return [item["embedding"] for item in data["data"]]
                else:
                    self._stats["errors"] += 1
                    return [[] for _ in texts]
        except Exception as e:
            self._stats["errors"] += 1
            log.warning(f"Embed batch error: {e}")
            return [[] for _ in texts]

    async def embed_texts(self, requests: list[EmbedRequest]) -> list[EmbedResult]:
        results: list[EmbedResult | None] = [None] * len(requests)
        to_embed: list[tuple[int, EmbedRequest]] = []

        # Check cache
        for i, req in enumerate(requests):
            cache_key = req.text[:80]
            if cache_key in self._cache:
                emb = self._cache[cache_key]
                results[i] = EmbedResult(
                    doc_id=req.doc_id,
                    chunk_index=req.chunk_index,
                    text=req.text,
                    embedding=emb,
                    model=self._model,
                    dim=len(emb),
                )
                self._stats["cache_hits"] += 1
            else:
                to_embed.append((i, req))

        if not to_embed:
            return [r for r in results if r is not None]

        # Process in batches with concurrency limit
        sem = asyncio.Semaphore(self._concurrency)
        async with aiohttp.ClientSession() as session:

            async def process_batch(batch: list[tuple[int, EmbedRequest]]):
                async with sem:
                    texts = [req.text for _, req in batch]
                    self._stats["batches_sent"] += 1
                    embeddings = await self._embed_batch(texts, session)
                    for (idx, req), emb in zip(batch, embeddings):
                        if emb:
                            result = EmbedResult(
                                doc_id=req.doc_id,
                                chunk_index=req.chunk_index,
                                text=req.text,
                                embedding=emb,
                                model=self._model,
                                dim=len(emb),
                            )
                            results[idx] = result
                            self._stats["texts_embedded"] += 1
                            self._cache[req.text[:80]] = emb
                            self._results.append(result)
                            if self._persist:
                                self._write_result(result)

            batches = [
                to_embed[i : i + self._batch_size]
                for i in range(0, len(to_embed), self._batch_size)
            ]
            await asyncio.gather(*[process_batch(b) for b in batches])

        return [r for r in results if r is not None]

    def _write_result(self, result: EmbedResult):
        try:
            with open(CACHE_FILE, "a") as f:
                f.write(json.dumps(result.to_dict(include_vector=True)) + "\n")
        except Exception:
            pass

    async def embed_document(
        self,
        text: str,
        doc_id: str = "",
        chunk_size: int = 512,
        overlap: int = 64,
    ) -> list[EmbedResult]:
        chunks = _chunk_text(text, chunk_size, overlap)
        requests = [
            EmbedRequest(text=chunk, doc_id=doc_id, chunk_index=i)
            for i, chunk in enumerate(chunks)
        ]
        return await self.embed_texts(requests)

    async def embed_single(self, text: str) -> list[float]:
        results = await self.embed_texts([EmbedRequest(text=text)])
        return results[0].embedding if results else []

    def search(
        self,
        query_embedding: list[float],
        top_k: int = 5,
        doc_id: str | None = None,
    ) -> list[tuple[EmbedResult, float]]:
        candidates = self._results
        if doc_id:
            candidates = [r for r in candidates if r.doc_id == doc_id]

        scored = []
        for result in candidates:
            if len(result.embedding) != len(query_embedding):
                continue
            score = _cosine_sim(query_embedding, result.embedding)
            scored.append((result, score))
        scored.sort(key=lambda x: -x[1])
        return scored[:top_k]

    def stats(self) -> dict:
        avg_ms = float(self._stats["total_ms"]) / max(
            int(self._stats["batches_sent"]), 1
        )
        return {
            **{k: v for k, v in self._stats.items()},
            "cached_texts": len(self._cache),
            "stored_results": len(self._results),
            "avg_batch_ms": round(avg_ms, 1),
            "model": self._model,
        }


def build_jarvis_batch_embedder() -> BatchEmbedder:
    return BatchEmbedder(
        embed_url=DEFAULT_EMBED_URL,
        model=DEFAULT_MODEL,
        batch_size=32,
        concurrency=4,
    )


async def main():
    import sys

    embedder = build_jarvis_batch_embedder()
    await embedder.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        texts = [
            "The JARVIS cluster runs on M1 and M2 GPU nodes with LM Studio.",
            "Redis is used for pub/sub messaging and distributed caching.",
            "GPU temperature monitoring prevents thermal throttling.",
            "The consensus engine uses multiple LLM models to validate outputs.",
            "Prompt injection attacks attempt to override system instructions.",
        ]
        print(f"Embedding {len(texts)} texts...")
        requests = [
            EmbedRequest(text=t, doc_id=f"doc_{i}") for i, t in enumerate(texts)
        ]
        results = await embedder.embed_texts(requests)

        print(f"\nResults ({len(results)} embedded):")
        for r in results:
            print(
                f"  doc={r.doc_id} chunk={r.chunk_index} dim={r.dim} text='{r.text[:50]}...'"
            )

        if results:
            query_emb = results[0].embedding
            hits = embedder.search(query_emb, top_k=3)
            print(f"\nSimilar to '{results[0].text[:40]}':")
            for hit, score in hits:
                print(f"  [{score:.4f}] {hit.text[:60]}")

        print(f"\nStats: {json.dumps(embedder.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(embedder.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
