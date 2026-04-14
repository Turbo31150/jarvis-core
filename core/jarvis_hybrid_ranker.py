#!/usr/bin/env python3
"""
jarvis_hybrid_ranker — Hybrid dense+sparse retrieval with Reciprocal Rank Fusion
Combines BM25 sparse scores with dense embedding similarity using RRF
"""

import asyncio
import json
import logging
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.hybrid_ranker")

REDIS_PREFIX = "jarvis:ranker:"


class FusionMethod(str, Enum):
    RRF = "rrf"  # Reciprocal Rank Fusion
    LINEAR = "linear"  # weighted linear combination
    MAX = "max"  # take max of normalised scores
    CONDORCET = "condorcet"  # pairwise dominance voting


@dataclass
class RankedResult:
    doc_id: str
    text: str
    source: str = ""
    sparse_score: float = 0.0
    dense_score: float = 0.0
    hybrid_score: float = 0.0
    sparse_rank: int = 0
    dense_rank: int = 0
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "hybrid_score": round(self.hybrid_score, 6),
            "sparse_score": round(self.sparse_score, 6),
            "dense_score": round(self.dense_score, 6),
            "sparse_rank": self.sparse_rank,
            "dense_rank": self.dense_rank,
            "source": self.source,
            "text": self.text[:200],
            "metadata": self.metadata,
        }


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _normalize_scores(results: list[tuple[str, float]]) -> dict[str, float]:
    """Min-max normalize scores to [0,1]."""
    if not results:
        return {}
    scores = [s for _, s in results]
    mn, mx = min(scores), max(scores)
    if mx == mn:
        return {doc_id: 1.0 for doc_id, _ in results}
    return {doc_id: (s - mn) / (mx - mn) for doc_id, s in results}


def _rrf_score(rank: int, k: int = 60) -> float:
    return 1.0 / (k + rank)


async def _embed_query(
    query: str,
    endpoint_url: str,
    model: str,
    timeout_s: float = 15.0,
) -> list[float]:
    payload = {"input": [query], "model": model}
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout_s)
        ) as sess:
            async with sess.post(f"{endpoint_url}/v1/embeddings", json=payload) as r:
                if r.status != 200:
                    return []
                data = await r.json(content_type=None)
                items = data.get("data", [])
                return items[0]["embedding"] if items else []
    except Exception as e:
        log.warning(f"Query embed failed: {e}")
        return []


class HybridRanker:
    def __init__(
        self,
        sparse_weight: float = 0.4,
        dense_weight: float = 0.6,
        fusion: FusionMethod = FusionMethod.RRF,
        rrf_k: int = 60,
        embed_url: str = "http://192.168.1.26:1234",
        embed_model: str = "nomic-embed-text",
    ):
        self.redis: aioredis.Redis | None = None
        self.sparse_weight = sparse_weight
        self.dense_weight = dense_weight
        self.fusion = fusion
        self.rrf_k = rrf_k
        self.embed_url = embed_url
        self.embed_model = embed_model
        # In-memory document store: doc_id → {text, embedding, source, metadata}
        self._store: dict[str, dict] = {}
        self._stats: dict[str, int] = {
            "queries": 0,
            "docs_stored": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def add_doc(
        self,
        doc_id: str,
        text: str,
        embedding: list[float],
        source: str = "",
        metadata: dict | None = None,
        sparse_scores: dict[str, float] | None = None,
    ):
        self._store[doc_id] = {
            "text": text,
            "embedding": embedding,
            "source": source,
            "metadata": metadata or {},
        }
        self._stats["docs_stored"] = len(self._store)

    def add_docs_bulk(self, docs: list[dict]):
        """docs: [{doc_id, text, embedding, source, metadata}]"""
        for d in docs:
            self.add_doc(
                d["doc_id"],
                d.get("text", ""),
                d.get("embedding", []),
                source=d.get("source", ""),
                metadata=d.get("metadata"),
            )

    def _dense_search(
        self, query_embedding: list[float], top_k: int
    ) -> list[tuple[str, float]]:
        if not query_embedding:
            return []
        scores = [
            (doc_id, _cosine(query_embedding, doc["embedding"]))
            for doc_id, doc in self._store.items()
            if doc["embedding"]
        ]
        scores.sort(key=lambda x: -x[1])
        return scores[: top_k * 3]

    def _sparse_search(
        self, query_terms: set[str], top_k: int
    ) -> list[tuple[str, float]]:
        """Simple TF-based sparse search (no BM25 to keep dependency-free)."""
        scores = []
        for doc_id, doc in self._store.items():
            text_lower = doc["text"].lower()
            score = sum(
                text_lower.count(t) / max(len(text_lower.split()), 1)
                for t in query_terms
            )
            if score > 0:
                scores.append((doc_id, score))
        scores.sort(key=lambda x: -x[1])
        return scores[: top_k * 3]

    def _fuse_rrf(
        self,
        sparse_ranked: list[tuple[str, float]],
        dense_ranked: list[tuple[str, float]],
    ) -> dict[str, float]:
        scores: dict[str, float] = {}
        for rank, (doc_id, _) in enumerate(sparse_ranked, 1):
            scores[doc_id] = scores.get(doc_id, 0.0) + self.sparse_weight * _rrf_score(
                rank, self.rrf_k
            )
        for rank, (doc_id, _) in enumerate(dense_ranked, 1):
            scores[doc_id] = scores.get(doc_id, 0.0) + self.dense_weight * _rrf_score(
                rank, self.rrf_k
            )
        return scores

    def _fuse_linear(
        self,
        sparse_ranked: list[tuple[str, float]],
        dense_ranked: list[tuple[str, float]],
    ) -> dict[str, float]:
        sparse_norm = _normalize_scores(sparse_ranked)
        dense_norm = _normalize_scores(dense_ranked)
        all_ids = set(sparse_norm) | set(dense_norm)
        return {
            doc_id: (
                self.sparse_weight * sparse_norm.get(doc_id, 0.0)
                + self.dense_weight * dense_norm.get(doc_id, 0.0)
            )
            for doc_id in all_ids
        }

    def _fuse_max(
        self,
        sparse_ranked: list[tuple[str, float]],
        dense_ranked: list[tuple[str, float]],
    ) -> dict[str, float]:
        sparse_norm = _normalize_scores(sparse_ranked)
        dense_norm = _normalize_scores(dense_ranked)
        all_ids = set(sparse_norm) | set(dense_norm)
        return {
            doc_id: max(sparse_norm.get(doc_id, 0.0), dense_norm.get(doc_id, 0.0))
            for doc_id in all_ids
        }

    async def search(
        self,
        query: str,
        top_k: int = 10,
        use_dense: bool = True,
        filter_fn: Any = None,
    ) -> list[RankedResult]:
        self._stats["queries"] += 1

        query_terms = {t for t in query.lower().split() if len(t) > 2}
        sparse_ranked = self._sparse_search(query_terms, top_k)

        dense_ranked: list[tuple[str, float]] = []
        if use_dense:
            query_emb = await _embed_query(query, self.embed_url, self.embed_model)
            dense_ranked = self._dense_search(query_emb, top_k)

        # Fuse
        if self.fusion == FusionMethod.RRF:
            fused = self._fuse_rrf(sparse_ranked, dense_ranked)
        elif self.fusion == FusionMethod.LINEAR:
            fused = self._fuse_linear(sparse_ranked, dense_ranked)
        else:
            fused = self._fuse_max(sparse_ranked, dense_ranked)

        # Rank lookup maps
        sparse_rank_map = {
            doc_id: rank for rank, (doc_id, _) in enumerate(sparse_ranked, 1)
        }
        dense_rank_map = {
            doc_id: rank for rank, (doc_id, _) in enumerate(dense_ranked, 1)
        }
        sparse_score_map = dict(sparse_ranked)
        dense_score_map = dict(dense_ranked)

        results = []
        for doc_id, hybrid_score in sorted(fused.items(), key=lambda x: -x[1])[:top_k]:
            doc = self._store.get(doc_id)
            if not doc:
                continue
            if filter_fn and not filter_fn(doc):
                continue
            results.append(
                RankedResult(
                    doc_id=doc_id,
                    text=doc["text"],
                    source=doc["source"],
                    sparse_score=sparse_score_map.get(doc_id, 0.0),
                    dense_score=dense_score_map.get(doc_id, 0.0),
                    hybrid_score=hybrid_score,
                    sparse_rank=sparse_rank_map.get(doc_id, 0),
                    dense_rank=dense_rank_map.get(doc_id, 0),
                    metadata=doc["metadata"],
                )
            )

        return results

    def stats(self) -> dict:
        return {
            **self._stats,
            "fusion": self.fusion.value,
            "sparse_weight": self.sparse_weight,
            "dense_weight": self.dense_weight,
        }


def build_jarvis_hybrid_ranker() -> HybridRanker:
    return HybridRanker(
        sparse_weight=0.4,
        dense_weight=0.6,
        fusion=FusionMethod.RRF,
        embed_url="http://192.168.1.26:1234",
        embed_model="nomic-embed-text",
    )


async def main():
    import sys

    ranker = build_jarvis_hybrid_ranker()
    await ranker.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        # Add docs with fake embeddings
        import random

        docs = [
            {
                "doc_id": "d1",
                "text": "JARVIS multi-agent GPU cluster orchestration system inference routing.",
                "source": "overview",
            },
            {
                "doc_id": "d2",
                "text": "Redis pub/sub messaging state caching distributed coordination.",
                "source": "redis",
            },
            {
                "doc_id": "d3",
                "text": "Trading crypto markets technical analysis strategies signals.",
                "source": "trading",
            },
            {
                "doc_id": "d4",
                "text": "Load balancer routes inference requests GPU nodes latency aware.",
                "source": "infra",
            },
            {
                "doc_id": "d5",
                "text": "Budget tracking token consumption cost daily limits per agent.",
                "source": "budget",
            },
        ]
        for d in docs:
            emb = [random.gauss(0, 1) for _ in range(128)]
            ranker.add_doc(d["doc_id"], d["text"], emb, source=d["source"])

        queries = ["GPU inference", "trading signals crypto", "budget cost agent"]
        for q in queries:
            results = await ranker.search(q, top_k=3, use_dense=False)
            print(f"Query: {q!r}")
            for r in results:
                print(
                    f"  [{r.doc_id}] hybrid={r.hybrid_score:.4f} sparse_rank={r.sparse_rank} | {r.text[:60]}"
                )
            print()

        print(f"Stats: {json.dumps(ranker.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(ranker.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
