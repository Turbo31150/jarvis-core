#!/usr/bin/env python3
"""
jarvis_embedding_indexer — Local vector index for semantic search
FAISS-free cosine similarity index with JSONL persistence and batch indexing
"""

import asyncio
import json
import logging
import math
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.embedding_indexer")

INDEX_FILE = Path("/home/turbo/IA/Core/jarvis/data/embedding_index.jsonl")
REDIS_PREFIX = "jarvis:emb:"
EMBED_URL = "http://192.168.1.26:1234"
EMBED_MODEL = "nomic-embed-text"
EMBED_DIM = 768


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    ma = math.sqrt(sum(x * x for x in a))
    mb = math.sqrt(sum(x * x for x in b))
    return dot / max(ma * mb, 1e-9)


def _normalize(v: list[float]) -> list[float]:
    mag = math.sqrt(sum(x * x for x in v))
    if mag < 1e-9:
        return v
    return [x / mag for x in v]


@dataclass
class IndexDocument:
    doc_id: str
    text: str
    embedding: list[float]
    metadata: dict = field(default_factory=dict)
    namespace: str = "default"
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "text": self.text[:500],
            "embedding": self.embedding,
            "metadata": self.metadata,
            "namespace": self.namespace,
            "ts": self.ts,
        }


@dataclass
class SearchResult:
    doc_id: str
    text: str
    score: float
    metadata: dict
    namespace: str

    def to_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "text": self.text[:300],
            "score": round(self.score, 4),
            "metadata": self.metadata,
            "namespace": self.namespace,
        }


class EmbeddingIndexer:
    def __init__(self, embed_url: str = EMBED_URL, embed_model: str = EMBED_MODEL):
        self.redis: aioredis.Redis | None = None
        self.embed_url = embed_url
        self.embed_model = embed_model
        self._index: dict[str, IndexDocument] = {}
        self._stats: dict[str, int] = {"indexed": 0, "searches": 0, "embed_calls": 0}
        INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self):
        if INDEX_FILE.exists():
            try:
                for line in INDEX_FILE.read_text().splitlines():
                    if line.strip():
                        d = json.loads(line)
                        doc = IndexDocument(
                            **{
                                k: v
                                for k, v in d.items()
                                if k in IndexDocument.__dataclass_fields__
                            }
                        )
                        self._index[doc.doc_id] = doc
                log.debug(f"Loaded {len(self._index)} documents from index")
            except Exception as e:
                log.warning(f"Index load error: {e}")

    def _persist(self, doc: IndexDocument):
        with open(INDEX_FILE, "a") as f:
            f.write(json.dumps(doc.to_dict()) + "\n")

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    async def _embed(self, text: str) -> list[float]:
        self._stats["embed_calls"] += 1
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            ) as sess:
                async with sess.post(
                    f"{self.embed_url}/v1/embeddings",
                    json={"model": self.embed_model, "input": text[:2048]},
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        emb = data["data"][0]["embedding"]
                        return _normalize(emb)
        except Exception as e:
            log.debug(f"Embed error: {e}")
        # Fallback: deterministic pseudo-embedding from text hash
        import hashlib

        h = hashlib.sha256(text.encode()).digest()
        emb = [(b / 128.0 - 1.0) for b in h[: EMBED_DIM % len(h) * len(h)]]
        emb = (emb * (EMBED_DIM // len(emb) + 1))[:EMBED_DIM]
        return _normalize(emb)

    async def add(
        self,
        text: str,
        doc_id: str | None = None,
        metadata: dict | None = None,
        namespace: str = "default",
        embedding: list[float] | None = None,
    ) -> IndexDocument:
        emb = embedding or await self._embed(text)
        doc = IndexDocument(
            doc_id=doc_id or str(uuid.uuid4())[:10],
            text=text,
            embedding=emb,
            metadata=metadata or {},
            namespace=namespace,
        )
        self._index[doc.doc_id] = doc
        self._persist(doc)
        self._stats["indexed"] += 1

        if self.redis:
            await self.redis.hset(
                f"{REDIS_PREFIX}meta:{namespace}",
                doc.doc_id,
                json.dumps({"text": text[:200], "metadata": metadata or {}}),
            )
        return doc

    async def add_batch(
        self,
        texts: list[str],
        metadata_list: list[dict] | None = None,
        namespace: str = "default",
    ) -> list[IndexDocument]:
        meta_list = metadata_list or [{} for _ in texts]
        tasks = [
            self.add(text, metadata=meta, namespace=namespace)
            for text, meta in zip(texts, meta_list)
        ]
        return await asyncio.gather(*tasks)

    async def search(
        self,
        query: str,
        top_k: int = 5,
        namespace: str | None = None,
        threshold: float = 0.0,
        query_embedding: list[float] | None = None,
    ) -> list[SearchResult]:
        self._stats["searches"] += 1
        q_emb = query_embedding or await self._embed(query)

        candidates = list(self._index.values())
        if namespace:
            candidates = [d for d in candidates if d.namespace == namespace]

        scores = [(_cosine(q_emb, doc.embedding), doc) for doc in candidates]
        scores.sort(key=lambda x: x[0], reverse=True)

        return [
            SearchResult(
                doc_id=doc.doc_id,
                text=doc.text,
                score=score,
                metadata=doc.metadata,
                namespace=doc.namespace,
            )
            for score, doc in scores[:top_k]
            if score >= threshold
        ]

    def delete(self, doc_id: str) -> bool:
        if doc_id in self._index:
            del self._index[doc_id]
            return True
        return False

    def get(self, doc_id: str) -> IndexDocument | None:
        return self._index.get(doc_id)

    def list_namespaces(self) -> list[str]:
        return list({d.namespace for d in self._index.values()})

    def namespace_stats(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for doc in self._index.values():
            counts[doc.namespace] = counts.get(doc.namespace, 0) + 1
        return counts

    def rebuild_index(self):
        """Rewrite index file from current in-memory state."""
        with open(INDEX_FILE, "w") as f:
            for doc in self._index.values():
                f.write(json.dumps(doc.to_dict()) + "\n")
        log.info(f"Index rebuilt: {len(self._index)} documents")

    def stats(self) -> dict:
        return {
            **self._stats,
            "total_documents": len(self._index),
            "namespaces": self.namespace_stats(),
        }


async def main():
    import sys

    indexer = EmbeddingIndexer()
    await indexer.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        docs = [
            (
                "Redis is an in-memory data store used as a cache and message broker.",
                {"source": "docs"},
            ),
            (
                "Python is a high-level programming language known for its simplicity.",
                {"source": "wiki"},
            ),
            (
                "GPU acceleration dramatically speeds up machine learning training.",
                {"source": "ml"},
            ),
            (
                "Docker containers package applications with all their dependencies.",
                {"source": "devops"},
            ),
            (
                "Kubernetes orchestrates containerized applications across clusters.",
                {"source": "devops"},
            ),
        ]

        print("Indexing documents...")
        for text, meta in docs:
            doc = await indexer.add(text, metadata=meta, namespace="knowledge")
            print(f"  [{doc.doc_id}] {text[:60]}")

        print("\nSearching: 'distributed caching systems'")
        results = await indexer.search(
            "distributed caching systems", top_k=3, namespace="knowledge"
        )
        for r in results:
            print(f"  [{r.score:.3f}] {r.text[:80]}")

        print(f"\nStats: {json.dumps(indexer.stats(), indent=2)}")

    elif cmd == "search" and len(sys.argv) > 2:
        query = " ".join(sys.argv[2:])
        results = await indexer.search(query, top_k=5)
        for r in results:
            print(f"  [{r.score:.3f}] [{r.namespace}] {r.text[:100]}")

    elif cmd == "stats":
        print(json.dumps(indexer.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
