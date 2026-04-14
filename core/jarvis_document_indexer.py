#!/usr/bin/env python3
"""
jarvis_document_indexer — Full-text and semantic document indexing with search
Index documents with embeddings, BM25 scoring, and hybrid retrieval
"""

import asyncio
import hashlib
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.document_indexer")

REDIS_PREFIX = "jarvis:docidx:"
INDEX_FILE = Path("/home/turbo/IA/Core/jarvis/data/doc_index.jsonl")
EMBED_URL = "http://192.168.1.26:1234"
EMBED_MODEL = "nomic-embed-text"


def _tokenize(text: str) -> list[str]:
    return re.findall(r"\b[a-zA-Z0-9_]{2,}\b", text.lower())


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    ma = math.sqrt(sum(x * x for x in a))
    mb = math.sqrt(sum(x * x for x in b))
    return dot / max(ma * mb, 1e-9)


@dataclass
class Document:
    doc_id: str
    title: str
    content: str
    namespace: str = "default"
    tags: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    embedding: list[float] = field(default_factory=list)
    ts: float = field(default_factory=time.time)
    chunk_ids: list[str] = field(default_factory=list)

    def to_dict(self, include_embedding: bool = False) -> dict:
        d = {
            "doc_id": self.doc_id,
            "title": self.title,
            "content": self.content[:500],
            "namespace": self.namespace,
            "tags": self.tags,
            "metadata": self.metadata,
            "ts": self.ts,
        }
        if include_embedding:
            d["embedding"] = self.embedding
        return d


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    text: str
    position: int
    embedding: list[float] = field(default_factory=list)


@dataclass
class SearchResult:
    doc_id: str
    title: str
    snippet: str
    score: float
    bm25_score: float
    semantic_score: float
    namespace: str
    tags: list[str]

    def to_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "title": self.title,
            "snippet": self.snippet,
            "score": round(self.score, 4),
            "bm25_score": round(self.bm25_score, 4),
            "semantic_score": round(self.semantic_score, 4),
        }


class BM25:
    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self._docs: dict[str, list[str]] = {}  # doc_id → tokens
        self._df: dict[str, int] = {}  # term → doc frequency
        self._avg_dl = 0.0

    def add(self, doc_id: str, text: str):
        tokens = _tokenize(text)
        self._docs[doc_id] = tokens
        for term in set(tokens):
            self._df[term] = self._df.get(term, 0) + 1
        self._avg_dl = sum(len(t) for t in self._docs.values()) / max(
            len(self._docs), 1
        )

    def remove(self, doc_id: str):
        if doc_id in self._docs:
            for term in set(self._docs[doc_id]):
                self._df[term] = max(0, self._df.get(term, 1) - 1)
            del self._docs[doc_id]

    def score(self, doc_id: str, query_tokens: list[str]) -> float:
        if doc_id not in self._docs:
            return 0.0
        N = len(self._docs)
        doc_tokens = self._docs[doc_id]
        dl = len(doc_tokens)
        s = 0.0
        tf_map: dict[str, int] = {}
        for t in doc_tokens:
            tf_map[t] = tf_map.get(t, 0) + 1
        for term in set(query_tokens):
            tf = tf_map.get(term, 0)
            df = self._df.get(term, 0)
            if tf == 0 or df == 0:
                continue
            idf = math.log((N - df + 0.5) / (df + 0.5) + 1)
            tf_norm = (
                tf
                * (self.k1 + 1)
                / (tf + self.k1 * (1 - self.b + self.b * dl / max(self._avg_dl, 1)))
            )
            s += idf * tf_norm
        return s


class DocumentIndexer:
    def __init__(self, chunk_size: int = 500, chunk_overlap: int = 50):
        self.redis: aioredis.Redis | None = None
        self._docs: dict[str, Document] = {}
        self._chunks: list[Chunk] = []
        self._bm25 = BM25()
        self._embed_cache: dict[str, list[float]] = {}
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._stats: dict[str, int] = {
            "indexed": 0,
            "searches": 0,
            "embed_calls": 0,
            "cache_hits": 0,
        }
        self._load()

    def _load(self):
        if INDEX_FILE.exists():
            try:
                for line in INDEX_FILE.read_text().splitlines():
                    if not line.strip():
                        continue
                    d = json.loads(line)
                    doc = Document(
                        doc_id=d["doc_id"],
                        title=d.get("title", ""),
                        content=d.get("content", ""),
                        namespace=d.get("namespace", "default"),
                        tags=d.get("tags", []),
                        metadata=d.get("metadata", {}),
                        embedding=d.get("embedding", []),
                        ts=d.get("ts", time.time()),
                    )
                    self._docs[doc.doc_id] = doc
                    self._bm25.add(doc.doc_id, doc.title + " " + doc.content)
                log.debug(f"Loaded {len(self._docs)} documents from index")
            except Exception as e:
                log.warning(f"Index load error: {e}")

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    async def _embed(self, text: str) -> list[float]:
        key = text[:200]
        if key in self._embed_cache:
            self._stats["cache_hits"] += 1
            return self._embed_cache[key]
        self._stats["embed_calls"] += 1
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=8)
            ) as sess:
                async with sess.post(
                    f"{EMBED_URL}/v1/embeddings",
                    json={"model": EMBED_MODEL, "input": text[:2048]},
                ) as r:
                    if r.status == 200:
                        data = await r.json(content_type=None)
                        emb = data["data"][0]["embedding"]
                        mag = math.sqrt(sum(x * x for x in emb))
                        if mag > 1e-9:
                            emb = [x / mag for x in emb]
                        self._embed_cache[key] = emb
                        return emb
        except Exception:
            pass
        # Fallback keyword embedding
        import hashlib as _hashlib

        words = text.lower().split()
        dim = 256
        vec = [0.0] * dim
        for word in set(words):
            h = int(_hashlib.md5(word.encode()).hexdigest()[:8], 16)
            vec[h % dim] += words.count(word) / max(len(words), 1)
        mag = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / mag for x in vec]

    def _chunk_text(self, text: str) -> list[str]:
        words = text.split()
        chunks = []
        step = self._chunk_size - self._chunk_overlap
        for i in range(0, len(words), step):
            chunk = " ".join(words[i : i + self._chunk_size])
            if chunk:
                chunks.append(chunk)
        return chunks or [text]

    async def index(
        self,
        title: str,
        content: str,
        doc_id: str | None = None,
        namespace: str = "default",
        tags: list[str] | None = None,
        metadata: dict | None = None,
    ) -> Document:

        did = doc_id or hashlib.md5(f"{title}{content[:100]}".encode()).hexdigest()[:12]

        # Create and embed document
        full_text = f"{title}\n\n{content}"
        embedding = await self._embed(full_text[:1000])

        doc = Document(
            doc_id=did,
            title=title,
            content=content,
            namespace=namespace,
            tags=tags or [],
            metadata=metadata or {},
            embedding=embedding,
        )
        self._docs[did] = doc
        self._bm25.add(did, title + " " + content)
        self._stats["indexed"] += 1

        # Persist
        with open(INDEX_FILE, "a") as f:
            f.write(json.dumps(doc.to_dict(include_embedding=True)) + "\n")

        log.debug(f"Indexed: {did} — {title[:50]}")
        return doc

    async def search(
        self,
        query: str,
        top_k: int = 5,
        namespace: str | None = None,
        tags: list[str] | None = None,
        alpha: float = 0.5,  # 0=BM25 only, 1=semantic only
    ) -> list[SearchResult]:
        self._stats["searches"] += 1
        q_tokens = _tokenize(query)
        q_emb = await self._embed(query)

        candidates = list(self._docs.values())
        if namespace:
            candidates = [d for d in candidates if d.namespace == namespace]
        if tags:
            candidates = [d for d in candidates if any(t in d.tags for t in tags)]

        results = []
        for doc in candidates:
            bm25_s = self._bm25.score(doc.doc_id, q_tokens)
            sem_s = (
                _cosine(q_emb, doc.embedding)
                if doc.embedding and len(doc.embedding) == len(q_emb)
                else 0.0
            )

            # Normalize BM25 to 0-1 range (rough)
            bm25_norm = min(bm25_s / 20.0, 1.0)
            combined = (1 - alpha) * bm25_norm + alpha * sem_s

            # Extract snippet
            words = doc.content.split()
            snippet = " ".join(words[:30]) + ("..." if len(words) > 30 else "")

            results.append(
                SearchResult(
                    doc_id=doc.doc_id,
                    title=doc.title,
                    snippet=snippet,
                    score=combined,
                    bm25_score=bm25_norm,
                    semantic_score=sem_s,
                    namespace=doc.namespace,
                    tags=doc.tags,
                )
            )

        results.sort(key=lambda r: -r.score)
        return results[:top_k]

    def delete(self, doc_id: str) -> bool:
        if doc_id in self._docs:
            del self._docs[doc_id]
            self._bm25.remove(doc_id)
            return True
        return False

    def list_docs(self, namespace: str | None = None) -> list[dict]:
        docs = self._docs.values()
        if namespace:
            docs = [d for d in docs if d.namespace == namespace]
        return [d.to_dict() for d in docs]

    def stats(self) -> dict:
        namespaces = list({d.namespace for d in self._docs.values()})
        return {**self._stats, "total_docs": len(self._docs), "namespaces": namespaces}


async def main():
    import sys

    indexer = DocumentIndexer()
    await indexer.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Indexing documents...")
        docs = [
            (
                "JARVIS Cluster Overview",
                "M1 node runs qwen3.5-9b and qwen3.5-27b. M2 runs deepseek-r1. Redis on localhost:6379.",
                "jarvis",
            ),
            (
                "GPU Memory Management",
                "VRAM allocation is handled by the GPU allocator. Each model reserves memory before loading.",
                "jarvis",
            ),
            (
                "Redis Configuration",
                "Redis defaults to port 6379. Use requirepass for authentication. AOF enabled for persistence.",
                "infra",
            ),
            (
                "Python Async Patterns",
                "Use asyncio.gather for parallel coroutines. asyncio.create_task for fire-and-forget.",
                "code",
            ),
            (
                "LLM Inference Pipeline",
                "Requests flow through: admission → rate limiter → router → load balancer → backend.",
                "jarvis",
            ),
        ]
        for title, content, ns in docs:
            doc = await indexer.index(title, content, namespace=ns)
            print(f"  [{doc.doc_id}] {title}")

        print("\nSearching: 'GPU memory'")
        results = await indexer.search("GPU memory", top_k=3)
        for r in results:
            print(f"  [{r.score:.3f}] {r.title}")
            print(f"         {r.snippet[:80]}")

        print(f"\nStats: {json.dumps(indexer.stats(), indent=2)}")

    elif cmd == "list":
        for d in indexer.list_docs():
            print(f"  {d['doc_id']:<15} [{d['namespace']}] {d['title']}")

    elif cmd == "stats":
        print(json.dumps(indexer.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
