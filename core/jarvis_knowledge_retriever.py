#!/usr/bin/env python3
"""
jarvis_knowledge_retriever — Hybrid knowledge retrieval (BM25 + dense vector)
Document ingestion, chunking, dual-index search, reranking, citation tracking
"""

import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

log = logging.getLogger("jarvis.knowledge_retriever")

CHARS_PER_TOKEN = 3.8


class RetrievalMode(str, Enum):
    BM25 = "bm25"
    DENSE = "dense"
    HYBRID = "hybrid"  # BM25 + dense, RRF fusion


@dataclass
class Document:
    doc_id: str
    content: str
    title: str = ""
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    @property
    def token_count(self) -> int:
        return max(1, int(len(self.content) / CHARS_PER_TOKEN))


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    content: str
    start_char: int = 0
    chunk_index: int = 0
    embedding: list[float] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def token_count(self) -> int:
        return max(1, int(len(self.content) / CHARS_PER_TOKEN))


@dataclass
class RetrievalResult:
    chunk: Chunk
    score: float
    score_bm25: float = 0.0
    score_dense: float = 0.0
    rank: int = 0
    doc_title: str = ""
    doc_source: str = ""

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk.chunk_id,
            "doc_id": self.chunk.doc_id,
            "doc_title": self.doc_title,
            "doc_source": self.doc_source,
            "score": round(self.score, 4),
            "score_bm25": round(self.score_bm25, 4),
            "score_dense": round(self.score_dense, 4),
            "rank": self.rank,
            "content_preview": self.chunk.content[:150],
        }


# --- BM25 implementation ---

_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "have",
        "has",
        "do",
        "does",
        "and",
        "or",
        "but",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "it",
        "this",
        "that",
        "with",
        "as",
    }
)


def _tokenize(text: str) -> list[str]:
    tokens = re.findall(r"\b\w+\b", text.lower())
    return [t for t in tokens if t not in _STOPWORDS and len(t) > 1]


class BM25Index:
    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self._chunks: dict[str, Chunk] = {}
        self._tf: dict[str, dict[str, int]] = {}  # chunk_id -> term -> count
        self._df: dict[str, int] = {}  # term -> doc_freq
        self._avgdl: float = 0.0

    def add(self, chunk: Chunk):
        tokens = _tokenize(chunk.content)
        self._chunks[chunk.chunk_id] = chunk
        tf: dict[str, int] = {}
        for t in tokens:
            tf[t] = tf.get(t, 0) + 1
        self._tf[chunk.chunk_id] = tf
        for t in set(tokens):
            self._df[t] = self._df.get(t, 0) + 1
        # Recompute avgdl
        total = sum(len(tf) for tf in self._tf.values())
        self._avgdl = total / max(len(self._tf), 1)

    def remove(self, chunk_id: str):
        if chunk_id not in self._chunks:
            return
        tf = self._tf.pop(chunk_id, {})
        for t in tf:
            self._df[t] = max(0, self._df.get(t, 1) - 1)
        del self._chunks[chunk_id]

    def score(self, query: str, chunk_id: str) -> float:
        terms = _tokenize(query)
        if not terms or chunk_id not in self._tf:
            return 0.0
        n = len(self._chunks)
        tf_map = self._tf[chunk_id]
        dl = sum(tf_map.values())
        score = 0.0
        for term in terms:
            tf = tf_map.get(term, 0)
            df = self._df.get(term, 0)
            if df == 0:
                continue
            idf = math.log((n - df + 0.5) / (df + 0.5) + 1)
            num = tf * (self.k1 + 1)
            denom = tf + self.k1 * (1 - self.b + self.b * dl / max(self._avgdl, 1))
            score += idf * num / max(denom, 1e-9)
        return score

    def search(self, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        scores = [(cid, self.score(query, cid)) for cid in self._chunks]
        scores.sort(key=lambda x: -x[1])
        return [(cid, s) for cid, s in scores[:top_k] if s > 0]

    def __len__(self) -> int:
        return len(self._chunks)


# --- Dense vector index (cosine brute-force) ---


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def _mock_embed(text: str, dim: int = 128) -> list[float]:
    import hashlib

    seed = int(hashlib.md5(text.encode()).hexdigest(), 16)
    vec = []
    for _ in range(dim):
        seed = (seed * 1664525 + 1013904223) & 0xFFFFFFFF
        vec.append((seed / 0xFFFFFFFF - 0.5) * 2)
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


# --- Chunker ---


def _chunk_text(
    content: str,
    chunk_size: int = 512,
    overlap: int = 64,
) -> list[tuple[str, int]]:
    """Split text into overlapping chunks. Returns (chunk_text, start_char)."""
    words = content.split()
    chunks = []
    i = 0
    while i < len(words):
        chunk_words = words[i : i + chunk_size]
        chunk_text = " ".join(chunk_words)
        start_char = len(" ".join(words[:i]))
        chunks.append((chunk_text, start_char))
        i += chunk_size - overlap
    return chunks


# --- RRF fusion ---


def _rrf_score(ranks: list[int], k: int = 60) -> float:
    return sum(1.0 / (k + r) for r in ranks)


class KnowledgeRetriever:
    def __init__(
        self,
        embed_fn=None,
        chunk_size: int = 512,
        chunk_overlap: int = 64,
        embed_dim: int = 128,
    ):
        self._docs: dict[str, Document] = {}
        self._chunks: dict[str, Chunk] = {}
        self._bm25 = BM25Index()
        self._dense: dict[str, list[float]] = {}  # chunk_id -> embedding
        self._embed_fn = embed_fn or (lambda t: _mock_embed(t, embed_dim))
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._stats: dict[str, int] = {
            "docs_indexed": 0,
            "chunks_indexed": 0,
            "queries": 0,
        }

    def ingest(self, doc: Document) -> int:
        self._docs[doc.doc_id] = doc
        raw_chunks = _chunk_text(doc.content, self._chunk_size, self._chunk_overlap)
        count = 0
        for i, (text, start) in enumerate(raw_chunks):
            chunk_id = f"{doc.doc_id}:{i}"
            embedding = self._embed_fn(text)
            chunk = Chunk(
                chunk_id=chunk_id,
                doc_id=doc.doc_id,
                content=text,
                start_char=start,
                chunk_index=i,
                embedding=embedding,
                metadata={"title": doc.title, "source": doc.source},
            )
            self._chunks[chunk_id] = chunk
            self._bm25.add(chunk)
            self._dense[chunk_id] = embedding
            count += 1

        self._stats["docs_indexed"] += 1
        self._stats["chunks_indexed"] += count
        log.debug(f"Indexed doc {doc.doc_id!r}: {count} chunks")
        return count

    def ingest_many(self, docs: list[Document]) -> int:
        return sum(self.ingest(d) for d in docs)

    def remove(self, doc_id: str) -> int:
        if doc_id not in self._docs:
            return 0
        del self._docs[doc_id]
        chunk_ids = [cid for cid in self._chunks if cid.startswith(f"{doc_id}:")]
        for cid in chunk_ids:
            self._bm25.remove(cid)
            self._dense.pop(cid, None)
            del self._chunks[cid]
        return len(chunk_ids)

    def _search_bm25(self, query: str, top_k: int) -> list[RetrievalResult]:
        hits = self._bm25.search(query, top_k)
        results = []
        for rank, (cid, score) in enumerate(hits, 1):
            chunk = self._chunks.get(cid)
            if not chunk:
                continue
            doc = self._docs.get(chunk.doc_id)
            results.append(
                RetrievalResult(
                    chunk=chunk,
                    score=score,
                    score_bm25=score,
                    rank=rank,
                    doc_title=doc.title if doc else "",
                    doc_source=doc.source if doc else "",
                )
            )
        return results

    def _search_dense(self, query: str, top_k: int) -> list[RetrievalResult]:
        q_emb = self._embed_fn(query)
        scored = [(cid, _cosine(q_emb, emb)) for cid, emb in self._dense.items()]
        scored.sort(key=lambda x: -x[1])
        results = []
        for rank, (cid, score) in enumerate(scored[:top_k], 1):
            chunk = self._chunks.get(cid)
            if not chunk:
                continue
            doc = self._docs.get(chunk.doc_id)
            results.append(
                RetrievalResult(
                    chunk=chunk,
                    score=score,
                    score_dense=score,
                    rank=rank,
                    doc_title=doc.title if doc else "",
                    doc_source=doc.source if doc else "",
                )
            )
        return results

    def _rrf_fusion(
        self,
        bm25_results: list[RetrievalResult],
        dense_results: list[RetrievalResult],
        top_k: int,
    ) -> list[RetrievalResult]:
        rrf: dict[str, dict] = {}
        for r in bm25_results:
            cid = r.chunk.chunk_id
            if cid not in rrf:
                rrf[cid] = {"result": r, "ranks": []}
            rrf[cid]["ranks"].append(r.rank)
            rrf[cid]["bm25"] = r.score_bm25

        for r in dense_results:
            cid = r.chunk.chunk_id
            if cid not in rrf:
                rrf[cid] = {"result": r, "ranks": []}
            rrf[cid]["ranks"].append(r.rank)
            rrf[cid]["dense"] = r.score_dense

        fused = []
        for cid, data in rrf.items():
            rrf_s = _rrf_score(data["ranks"])
            res = data["result"]
            res.score = rrf_s
            res.score_bm25 = data.get("bm25", 0.0)
            res.score_dense = data.get("dense", 0.0)
            fused.append(res)

        fused.sort(key=lambda r: -r.score)
        for rank, r in enumerate(fused[:top_k], 1):
            r.rank = rank
        return fused[:top_k]

    def search(
        self,
        query: str,
        top_k: int = 5,
        mode: RetrievalMode = RetrievalMode.HYBRID,
        filter_doc_id: str | None = None,
    ) -> list[RetrievalResult]:
        self._stats["queries"] += 1

        if mode == RetrievalMode.BM25:
            results = self._search_bm25(query, top_k)
        elif mode == RetrievalMode.DENSE:
            results = self._search_dense(query, top_k)
        else:
            bm25 = self._search_bm25(query, top_k * 2)
            dense = self._search_dense(query, top_k * 2)
            results = self._rrf_fusion(bm25, dense, top_k)

        if filter_doc_id:
            results = [r for r in results if r.chunk.doc_id == filter_doc_id]

        return results[:top_k]

    def format_context(self, results: list[RetrievalResult]) -> str:
        """Format results into a context string for LLM injection."""
        parts = []
        for r in results:
            header = f"[{r.doc_title or r.chunk.doc_id}]"
            if r.doc_source:
                header += f" ({r.doc_source})"
            parts.append(f"{header}\n{r.chunk.content}")
        return "\n\n---\n\n".join(parts)

    def stats(self) -> dict:
        return {
            **self._stats,
            "total_chunks": len(self._chunks),
            "total_docs": len(self._docs),
            "bm25_terms": len(self._bm25._df),
        }


def build_jarvis_knowledge_retriever(embed_dim: int = 128) -> KnowledgeRetriever:
    return KnowledgeRetriever(chunk_size=400, chunk_overlap=50, embed_dim=embed_dim)


def main():
    import sys

    kr = build_jarvis_knowledge_retriever()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Knowledge retriever demo...\n")

        docs = [
            Document(
                doc_id="jarvis_arch",
                title="JARVIS Architecture",
                source="internal",
                content=(
                    "JARVIS is a multi-agent orchestration system running on a GPU cluster. "
                    "The cluster consists of M1, M2, and OL1 nodes. "
                    "M1 is the primary inference node with RTX 3090 and RTX 4090 GPUs. "
                    "M2 provides overflow capacity. OL1 runs Ollama for local models. "
                    "The system uses Redis for inter-agent communication. "
                    "Load balancing is handled by the LLM router component."
                ),
            ),
            Document(
                doc_id="trading_docs",
                title="Trading Pipeline",
                source="internal",
                content=(
                    "The JARVIS trading pipeline connects to MEXC futures. "
                    "It analyzes BTC, ETH, and SOL pairs using technical indicators. "
                    "RSI, MACD, and Bollinger Bands are computed for signal generation. "
                    "Risk management enforces stop-loss at 2% per trade. "
                    "The pipeline runs on Hyperliquid for execution."
                ),
            ),
            Document(
                doc_id="gpu_guide",
                title="GPU Management",
                source="internal",
                content=(
                    "GPU memory management in JARVIS uses VRAM tracking. "
                    "Models are loaded on demand and evicted when memory is scarce. "
                    "Thermal monitoring alerts when GPU temperature exceeds 85°C. "
                    "The scheduler assigns inference tasks to the GPU with most free VRAM. "
                    "PBO overclocking is configured for RTX cards on M1."
                ),
            ),
        ]

        total = kr.ingest_many(docs)
        print(f"  Indexed {total} chunks from {len(docs)} documents\n")

        queries = [
            "What GPUs does M1 have?",
            "How does the trading pipeline work?",
            "GPU temperature monitoring",
        ]

        for query in queries:
            results = kr.search(query, top_k=2, mode=RetrievalMode.HYBRID)
            print(f"  Q: {query}")
            for r in results:
                print(
                    f"    [{r.rank}] {r.doc_title:<20} "
                    f"bm25={r.score_bm25:.3f} dense={r.score_dense:.3f} "
                    f"rrf={r.score:.4f}"
                )
                print(f"        {r.chunk.content[:80]}")
            print()

        print(f"Stats: {json.dumps(kr.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(kr.stats(), indent=2))


if __name__ == "__main__":
    main()
