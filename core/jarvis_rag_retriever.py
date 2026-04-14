#!/usr/bin/env python3
"""
jarvis_rag_retriever — Retrieval-Augmented Generation pipeline
Retrieves relevant documents, reranks, formats context for LLM injection
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

log = logging.getLogger("jarvis.rag_retriever")

REDIS_PREFIX = "jarvis:rag:"
EMBED_URL = "http://192.168.1.26:1234"
EMBED_MODEL = "nomic-embed-text"
INDEX_FILE = Path("/home/turbo/IA/Core/jarvis/data/embedding_index.jsonl")


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    ma = math.sqrt(sum(x * x for x in a))
    mb = math.sqrt(sum(x * x for x in b))
    return dot / max(ma * mb, 1e-9)


@dataclass
class Document:
    doc_id: str
    text: str
    embedding: list[float]
    metadata: dict = field(default_factory=dict)
    namespace: str = "default"
    score: float = 0.0

    def to_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "text": self.text[:400],
            "metadata": self.metadata,
            "namespace": self.namespace,
            "score": round(self.score, 4),
        }


@dataclass
class RetrievalResult:
    query: str
    documents: list[Document]
    context: str
    duration_ms: float
    embed_used: bool = True

    def to_dict(self) -> dict:
        return {
            "query": self.query[:200],
            "doc_count": len(self.documents),
            "context_chars": len(self.context),
            "duration_ms": round(self.duration_ms, 1),
            "embed_used": self.embed_used,
            "documents": [d.to_dict() for d in self.documents],
        }


class RAGRetriever:
    def __init__(
        self,
        embed_url: str = EMBED_URL,
        embed_model: str = EMBED_MODEL,
        top_k: int = 5,
        min_score: float = 0.3,
        context_template: str = "CONTEXT:\n{docs}\n\nQUERY: {query}",
    ):
        self.redis: aioredis.Redis | None = None
        self.embed_url = embed_url
        self.embed_model = embed_model
        self.top_k = top_k
        self.min_score = min_score
        self.context_template = context_template
        self._index: list[Document] = []
        self._stats: dict[str, int] = {
            "retrievals": 0,
            "embed_calls": 0,
            "cache_hits": 0,
        }
        self._embed_cache: dict[str, list[float]] = {}
        self._load_index()

    def _load_index(self):
        if INDEX_FILE.exists():
            try:
                for line in INDEX_FILE.read_text().splitlines():
                    if not line.strip():
                        continue
                    d = json.loads(line)
                    if "embedding" in d and d["embedding"]:
                        self._index.append(
                            Document(
                                doc_id=d.get("doc_id", ""),
                                text=d.get("text", ""),
                                embedding=d["embedding"],
                                metadata=d.get("metadata", {}),
                                namespace=d.get("namespace", "default"),
                            )
                        )
                log.debug(f"RAG index loaded: {len(self._index)} documents")
            except Exception as e:
                log.warning(f"Index load error: {e}")

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    async def embed(self, text: str) -> list[float]:
        cache_key = text[:200]
        if cache_key in self._embed_cache:
            self._stats["cache_hits"] += 1
            return self._embed_cache[cache_key]

        self._stats["embed_calls"] += 1
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=8)
            ) as sess:
                async with sess.post(
                    f"{self.embed_url}/v1/embeddings",
                    json={"model": self.embed_model, "input": text[:2048]},
                ) as r:
                    if r.status == 200:
                        data = await r.json(content_type=None)
                        emb = data["data"][0]["embedding"]
                        # Normalize
                        mag = math.sqrt(sum(x * x for x in emb))
                        if mag > 1e-9:
                            emb = [x / mag for x in emb]
                        self._embed_cache[cache_key] = emb
                        return emb
        except Exception as e:
            log.debug(f"Embed error: {e}")

        # Fallback: keyword-based pseudo-embedding
        return self._keyword_embed(text)

    def _keyword_embed(self, text: str) -> list[float]:
        """Fallback TF-IDF-like sparse embedding over vocabulary."""
        import hashlib

        words = text.lower().split()
        dim = 256
        vec = [0.0] * dim
        for word in set(words):
            h = int(hashlib.md5(word.encode()).hexdigest()[:8], 16)
            idx = h % dim
            vec[idx] += words.count(word) / max(len(words), 1)
        mag = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / mag for x in vec]

    async def add(
        self,
        text: str,
        doc_id: str | None = None,
        metadata: dict | None = None,
        namespace: str = "default",
    ) -> Document:
        import uuid

        emb = await self.embed(text)
        doc = Document(
            doc_id=doc_id or str(uuid.uuid4())[:8],
            text=text,
            embedding=emb,
            metadata=metadata or {},
            namespace=namespace,
        )
        self._index.append(doc)
        # Persist
        with open(INDEX_FILE, "a") as f:
            f.write(
                json.dumps(
                    {
                        "doc_id": doc.doc_id,
                        "text": doc.text[:500],
                        "embedding": doc.embedding,
                        "metadata": doc.metadata,
                        "namespace": doc.namespace,
                        "ts": time.time(),
                    }
                )
                + "\n"
            )
        return doc

    async def retrieve(
        self,
        query: str,
        top_k: int | None = None,
        namespace: str | None = None,
        min_score: float | None = None,
        rerank: bool = True,
    ) -> RetrievalResult:
        t0 = time.time()
        self._stats["retrievals"] += 1
        k = top_k or self.top_k
        threshold = min_score if min_score is not None else self.min_score

        q_emb = await self.embed(query)
        embed_used = len(q_emb) > 0

        candidates = self._index
        if namespace:
            candidates = [d for d in candidates if d.namespace == namespace]

        # Score all candidates
        scored = []
        for doc in candidates:
            if len(doc.embedding) == len(q_emb):
                score = _cosine(q_emb, doc.embedding)
            else:
                score = 0.0
            if score >= threshold:
                doc_copy = Document(
                    doc_id=doc.doc_id,
                    text=doc.text,
                    embedding=doc.embedding,
                    metadata=doc.metadata,
                    namespace=doc.namespace,
                    score=score,
                )
                scored.append(doc_copy)

        scored.sort(key=lambda d: -d.score)
        results = scored[:k]

        if rerank and len(results) > 1:
            results = self._rerank(query, results)

        context = self._format_context(query, results)

        return RetrievalResult(
            query=query,
            documents=results,
            context=context,
            duration_ms=(time.time() - t0) * 1000,
            embed_used=embed_used,
        )

    def _rerank(self, query: str, docs: list[Document]) -> list[Document]:
        """Simple keyword-overlap reranker on top of vector scores."""
        query_words = set(query.lower().split())
        for doc in docs:
            doc_words = set(doc.text.lower().split())
            overlap = len(query_words & doc_words) / max(len(query_words), 1)
            doc.score = 0.7 * doc.score + 0.3 * overlap
        docs.sort(key=lambda d: -d.score)
        return docs

    def _format_context(self, query: str, docs: list[Document]) -> str:
        if not docs:
            return ""
        doc_texts = "\n\n".join(
            f"[{i + 1}] (score={d.score:.3f}) {d.text}" for i, d in enumerate(docs)
        )
        return self.context_template.format(docs=doc_texts, query=query)

    async def retrieve_and_inject(
        self, messages: list[dict], query: str | None = None, **kwargs
    ) -> tuple[list[dict], RetrievalResult]:
        """Retrieve and prepend context to messages."""
        if query is None:
            # Use last user message as query
            for msg in reversed(messages):
                if msg.get("role") == "user":
                    query = msg.get("content", "")[:500]
                    break
        if not query:
            empty = RetrievalResult(query="", documents=[], context="", duration_ms=0)
            return messages, empty

        result = await self.retrieve(query, **kwargs)
        if not result.context:
            return messages, result

        # Inject context as system message before last user message
        injected = list(messages)
        for i in range(len(injected) - 1, -1, -1):
            if injected[i].get("role") == "user":
                injected.insert(i, {"role": "system", "content": result.context})
                break
        return injected, result

    def index_size(self) -> int:
        return len(self._index)

    def stats(self) -> dict:
        return {
            **self._stats,
            "index_size": len(self._index),
            "embed_cache_size": len(self._embed_cache),
            "namespaces": list({d.namespace for d in self._index}),
        }


async def main():
    import sys

    retriever = RAGRetriever(top_k=3, min_score=0.1)
    await retriever.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        # Add some documents
        docs = [
            (
                "M1 is the primary inference node with 3 GPUs running qwen3.5-9b and qwen3.5-27b.",
                {"source": "cluster"},
            ),
            (
                "M2 runs deepseek-r1-0528 and nomic-embed-text for embeddings.",
                {"source": "cluster"},
            ),
            (
                "Redis is used for caching, pub/sub, and distributed locking in JARVIS.",
                {"source": "arch"},
            ),
            (
                "The GPU allocator assigns VRAM to models before loading them.",
                {"source": "code"},
            ),
            (
                "Python asyncio enables concurrent LLM requests without blocking.",
                {"source": "code"},
            ),
        ]
        print("Indexing documents...")
        for text, meta in docs:
            doc = await retriever.add(text, metadata=meta, namespace="jarvis")
            print(f"  [{doc.doc_id}] {text[:60]}")

        print(f"\nIndex size: {retriever.index_size()}")

        # Retrieve
        query = "what models run on M1?"
        print(f"\nQuery: '{query}'")
        result = await retriever.retrieve(query, namespace="jarvis")
        for doc in result.documents:
            print(f"  [{doc.score:.3f}] {doc.text[:80]}")
        print(f"Duration: {result.duration_ms:.0f}ms")
        print(f"\nStats: {json.dumps(retriever.stats(), indent=2)}")

    elif cmd == "search" and len(sys.argv) > 2:
        query = " ".join(sys.argv[2:])
        result = await retriever.retrieve(query)
        for doc in result.documents:
            print(f"  [{doc.score:.3f}] [{doc.namespace}] {doc.text[:100]}")

    elif cmd == "stats":
        print(json.dumps(retriever.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
