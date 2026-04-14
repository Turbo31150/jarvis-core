#!/usr/bin/env python3
"""
jarvis_index_manager — Vector and keyword index lifecycle management
Create, update, delete, rebuild indexes; namespace isolation; stats tracking
"""

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.index_manager")

REDIS_PREFIX = "jarvis:idx:"


class IndexKind(str, Enum):
    VECTOR = "vector"  # dense embeddings
    KEYWORD = "keyword"  # BM25 / inverted index
    HYBRID = "hybrid"  # vector + keyword
    GRAPH = "graph"  # entity relationship graph


class IndexStatus(str, Enum):
    EMPTY = "empty"
    BUILDING = "building"
    READY = "ready"
    STALE = "stale"  # needs rebuild
    ERROR = "error"


@dataclass
class IndexConfig:
    name: str
    kind: IndexKind
    namespace: str = "default"
    dimensions: int = 384  # for VECTOR
    metric: str = "cosine"  # cosine | euclidean | dot
    max_vectors: int = 100_000
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class VectorRecord:
    doc_id: str
    vector: list[float]
    payload: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)


@dataclass
class SearchResult:
    doc_id: str
    score: float
    payload: dict[str, Any]
    index_name: str = ""

    def to_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "score": round(self.score, 6),
            "payload": self.payload,
            "index_name": self.index_name,
        }


@dataclass
class IndexStats:
    name: str
    kind: IndexKind
    status: IndexStatus
    vector_count: int = 0
    last_updated: float = 0.0
    last_rebuilt: float = 0.0
    build_duration_ms: float = 0.0
    namespace: str = "default"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind.value,
            "status": self.status.value,
            "vector_count": self.vector_count,
            "last_updated": self.last_updated,
            "last_rebuilt": self.last_rebuilt,
            "build_duration_ms": round(self.build_duration_ms, 2),
            "namespace": self.namespace,
        }


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def _euclidean_sim(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dist = math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))
    return 1.0 / (1.0 + dist)


def _dot(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


class VectorIndex:
    def __init__(self, config: IndexConfig):
        self.config = config
        self._records: dict[str, VectorRecord] = {}
        self._status = IndexStatus.EMPTY
        self._stats = IndexStats(
            name=config.name,
            kind=config.kind,
            status=IndexStatus.EMPTY,
            namespace=config.namespace,
        )

    def _score(self, a: list[float], b: list[float]) -> float:
        if self.config.metric == "cosine":
            return _cosine(a, b)
        elif self.config.metric == "euclidean":
            return _euclidean_sim(a, b)
        else:
            return _dot(a, b)

    def upsert(self, record: VectorRecord):
        if len(self._records) >= self.config.max_vectors:
            # Evict oldest
            oldest = min(self._records, key=lambda k: self._records[k].ts)
            del self._records[oldest]
        self._records[record.doc_id] = record
        self._stats.vector_count = len(self._records)
        self._stats.last_updated = time.time()
        self._status = IndexStatus.READY
        self._stats.status = IndexStatus.READY

    def delete(self, doc_id: str) -> bool:
        if doc_id in self._records:
            del self._records[doc_id]
            self._stats.vector_count = len(self._records)
            return True
        return False

    def search(
        self,
        query_vector: list[float],
        top_k: int = 10,
        filter_fn=None,
    ) -> list[SearchResult]:
        results = []
        for doc_id, rec in self._records.items():
            if filter_fn and not filter_fn(rec.payload):
                continue
            score = self._score(query_vector, rec.vector)
            results.append(
                SearchResult(
                    doc_id=doc_id,
                    score=score,
                    payload=rec.payload,
                    index_name=self.config.name,
                )
            )
        results.sort(key=lambda r: -r.score)
        return results[:top_k]

    def clear(self):
        self._records.clear()
        self._status = IndexStatus.EMPTY
        self._stats.status = IndexStatus.EMPTY
        self._stats.vector_count = 0

    def rebuild(self) -> float:
        t0 = time.time()
        self._status = IndexStatus.BUILDING
        # In a real implementation: rebuild ANN index (HNSW etc.)
        self._status = IndexStatus.READY
        dur = (time.time() - t0) * 1000
        self._stats.status = IndexStatus.READY
        self._stats.last_rebuilt = time.time()
        self._stats.build_duration_ms = dur
        return dur

    def stats(self) -> IndexStats:
        self._stats.vector_count = len(self._records)
        self._stats.status = self._status
        return self._stats


class IndexManager:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._indexes: dict[str, VectorIndex] = {}
        self._stats: dict[str, int] = {
            "upserts": 0,
            "deletes": 0,
            "searches": 0,
            "rebuilds": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def create(self, config: IndexConfig) -> VectorIndex:
        idx = VectorIndex(config)
        self._indexes[config.name] = idx
        log.info(
            f"Index created: {config.name!r} kind={config.kind.value} ns={config.namespace}"
        )
        return idx

    def get(self, name: str) -> VectorIndex | None:
        return self._indexes.get(name)

    def drop(self, name: str) -> bool:
        if name in self._indexes:
            del self._indexes[name]
            log.info(f"Index dropped: {name!r}")
            return True
        return False

    def upsert(self, index_name: str, record: VectorRecord) -> bool:
        idx = self._indexes.get(index_name)
        if not idx:
            return False
        idx.upsert(record)
        self._stats["upserts"] += 1
        if self.redis:
            asyncio.create_task(self._redis_upsert(index_name, record))
        return True

    def upsert_many(self, index_name: str, records: list[VectorRecord]) -> int:
        idx = self._indexes.get(index_name)
        if not idx:
            return 0
        for r in records:
            idx.upsert(r)
        self._stats["upserts"] += len(records)
        return len(records)

    def delete(self, index_name: str, doc_id: str) -> bool:
        idx = self._indexes.get(index_name)
        if not idx:
            return False
        ok = idx.delete(doc_id)
        if ok:
            self._stats["deletes"] += 1
        return ok

    def search(
        self,
        index_name: str,
        query_vector: list[float],
        top_k: int = 10,
        filter_fn=None,
    ) -> list[SearchResult]:
        idx = self._indexes.get(index_name)
        if not idx:
            return []
        self._stats["searches"] += 1
        return idx.search(query_vector, top_k, filter_fn)

    def multi_search(
        self,
        index_names: list[str],
        query_vector: list[float],
        top_k: int = 10,
    ) -> list[SearchResult]:
        """Search across multiple indexes, merge and re-rank."""
        all_results: list[SearchResult] = []
        for name in index_names:
            all_results.extend(self.search(name, query_vector, top_k))
        all_results.sort(key=lambda r: -r.score)
        return all_results[:top_k]

    def rebuild(self, index_name: str) -> float:
        idx = self._indexes.get(index_name)
        if not idx:
            return 0.0
        dur = idx.rebuild()
        self._stats["rebuilds"] += 1
        log.info(f"Index {index_name!r} rebuilt in {dur:.1f}ms")
        return dur

    def rebuild_all(self) -> dict[str, float]:
        return {name: self.rebuild(name) for name in self._indexes}

    async def _redis_upsert(self, index_name: str, record: VectorRecord):
        if not self.redis:
            return
        try:
            key = f"{REDIS_PREFIX}{index_name}:{record.doc_id}"
            payload = json.dumps(
                {
                    "doc_id": record.doc_id,
                    "payload": record.payload,
                    "ts": record.ts,
                }
            )
            await self.redis.setex(key, 86400, payload)
        except Exception:
            pass

    def list_indexes(self) -> list[dict]:
        return [idx.stats().to_dict() for idx in self._indexes.values()]

    def stats(self) -> dict:
        return {
            **self._stats,
            "indexes": len(self._indexes),
            "total_vectors": sum(
                idx.stats().vector_count for idx in self._indexes.values()
            ),
        }


def build_jarvis_index_manager() -> IndexManager:
    mgr = IndexManager()

    mgr.create(
        IndexConfig(
            name="inference.queries",
            kind=IndexKind.VECTOR,
            namespace="inference",
            dimensions=384,
            metric="cosine",
            max_vectors=50_000,
            description="Semantic index of inference queries",
        )
    )
    mgr.create(
        IndexConfig(
            name="knowledge.docs",
            kind=IndexKind.HYBRID,
            namespace="knowledge",
            dimensions=384,
            metric="cosine",
            max_vectors=100_000,
            description="JARVIS knowledge base documents",
        )
    )
    mgr.create(
        IndexConfig(
            name="trading.signals",
            kind=IndexKind.VECTOR,
            namespace="trading",
            dimensions=128,
            metric="cosine",
            max_vectors=10_000,
        )
    )

    return mgr


async def main():
    import sys
    import random

    mgr = build_jarvis_index_manager()
    await mgr.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Index manager demo...\n")

        rng = random.Random(42)

        # Upsert vectors
        def rand_vec(dim: int) -> list[float]:
            v = [rng.gauss(0, 1) for _ in range(dim)]
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            return [x / norm for x in v]

        for i in range(20):
            mgr.upsert(
                "inference.queries",
                VectorRecord(
                    doc_id=f"query-{i}",
                    vector=rand_vec(384),
                    payload={"text": f"query text {i}", "model": "qwen3.5"},
                ),
            )

        # Search
        query_vec = rand_vec(384)
        results = mgr.search("inference.queries", query_vec, top_k=5)
        print("  Search results (top 5):")
        for r in results:
            print(f"    {r.doc_id:<15} score={r.score:.4f}")

        # Multi-search
        for i in range(5):
            mgr.upsert(
                "knowledge.docs",
                VectorRecord(
                    doc_id=f"doc-{i}",
                    vector=rand_vec(384),
                    payload={"title": f"Document {i}"},
                ),
            )
        multi = mgr.multi_search(
            ["inference.queries", "knowledge.docs"], query_vec, top_k=3
        )
        print("\n  Multi-index search (top 3):")
        for r in multi:
            print(f"    {r.index_name:<25} {r.doc_id:<15} score={r.score:.4f}")

        # Rebuild
        dur = mgr.rebuild("inference.queries")
        print(f"\n  Rebuild inference.queries: {dur:.1f}ms")

        print(f"\nIndexes: {json.dumps(mgr.list_indexes(), indent=2)}")
        print(f"\nStats: {json.dumps(mgr.stats(), indent=2)}")

    elif cmd == "list":
        for idx in mgr.list_indexes():
            print(
                f"  {idx['name']:<30} {idx['kind']:<10} vectors={idx['vector_count']} status={idx['status']}"
            )

    elif cmd == "stats":
        print(json.dumps(mgr.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
