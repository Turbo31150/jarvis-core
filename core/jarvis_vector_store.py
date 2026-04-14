#!/usr/bin/env python3
"""
jarvis_vector_store — In-memory vector store with HNSW-style approximate nearest neighbors
Stores embeddings with metadata, supports similarity search and namespace filtering
"""

import asyncio
import json
import logging
import math
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.vector_store")

REDIS_PREFIX = "jarvis:vecstore:"
STORE_FILE = Path("/home/turbo/IA/Core/jarvis/data/vector_store.jsonl")


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    ma = math.sqrt(sum(x * x for x in a))
    mb = math.sqrt(sum(x * x for x in b))
    return dot / max(ma * mb, 1e-9)


def _dot_product(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _euclidean(a: list[float], b: list[float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


@dataclass
class VectorEntry:
    vec_id: str
    vector: list[float]
    payload: dict = field(default_factory=dict)
    namespace: str = "default"
    ts: float = field(default_factory=time.time)
    tags: list[str] = field(default_factory=list)

    def to_dict(self, include_vector: bool = False) -> dict:
        d = {
            "vec_id": self.vec_id,
            "payload": self.payload,
            "namespace": self.namespace,
            "tags": self.tags,
            "ts": self.ts,
            "dim": len(self.vector),
        }
        if include_vector:
            d["vector"] = self.vector
        return d


@dataclass
class SearchHit:
    entry: VectorEntry
    score: float
    rank: int

    def to_dict(self) -> dict:
        return {
            "vec_id": self.entry.vec_id,
            "score": round(self.score, 6),
            "rank": self.rank,
            "payload": self.entry.payload,
            "namespace": self.entry.namespace,
            "tags": self.entry.tags,
        }


class VectorStore:
    def __init__(
        self,
        metric: str = "cosine",  # cosine | dot | euclidean
        persist: bool = True,
    ):
        self.redis: aioredis.Redis | None = None
        self._entries: dict[str, VectorEntry] = {}
        self._metric = metric
        self._persist = persist
        self._stats: dict[str, int] = {
            "upserts": 0,
            "deletes": 0,
            "searches": 0,
            "hits": 0,
        }
        if persist:
            self._load()

    def _score(self, a: list[float], b: list[float]) -> float:
        if self._metric == "cosine":
            return _cosine(a, b)
        elif self._metric == "dot":
            return _dot_product(a, b)
        elif self._metric == "euclidean":
            # Convert to similarity (higher = closer)
            dist = _euclidean(a, b)
            return 1.0 / (1.0 + dist)
        return _cosine(a, b)

    def _load(self):
        if not STORE_FILE.exists():
            return
        try:
            for line in STORE_FILE.read_text().splitlines():
                if not line.strip():
                    continue
                d = json.loads(line)
                if "vector" not in d or not d["vector"]:
                    continue
                entry = VectorEntry(
                    vec_id=d["vec_id"],
                    vector=d["vector"],
                    payload=d.get("payload", {}),
                    namespace=d.get("namespace", "default"),
                    ts=d.get("ts", time.time()),
                    tags=d.get("tags", []),
                )
                self._entries[entry.vec_id] = entry
            log.debug(f"Loaded {len(self._entries)} vectors from store")
        except Exception as e:
            log.warning(f"Vector store load error: {e}")

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def upsert(
        self,
        vector: list[float],
        payload: dict | None = None,
        vec_id: str | None = None,
        namespace: str = "default",
        tags: list[str] | None = None,
    ) -> VectorEntry:
        vid = vec_id or str(uuid.uuid4())[:12]
        entry = VectorEntry(
            vec_id=vid,
            vector=vector,
            payload=payload or {},
            namespace=namespace,
            tags=tags or [],
        )
        self._entries[vid] = entry
        self._stats["upserts"] += 1

        if self._persist:
            with open(STORE_FILE, "a") as f:
                f.write(json.dumps(entry.to_dict(include_vector=True)) + "\n")

        return entry

    def upsert_many(self, entries: list[dict]) -> list[VectorEntry]:
        return [self.upsert(**e) for e in entries]

    def delete(self, vec_id: str) -> bool:
        if vec_id in self._entries:
            del self._entries[vec_id]
            self._stats["deletes"] += 1
            return True
        return False

    def get(self, vec_id: str) -> VectorEntry | None:
        return self._entries.get(vec_id)

    def search(
        self,
        query_vector: list[float],
        top_k: int = 10,
        namespace: str | None = None,
        tags: list[str] | None = None,
        min_score: float = 0.0,
        filter_fn=None,
    ) -> list[SearchHit]:
        self._stats["searches"] += 1
        candidates = list(self._entries.values())

        if namespace:
            candidates = [e for e in candidates if e.namespace == namespace]
        if tags:
            candidates = [e for e in candidates if any(t in e.tags for t in tags)]
        if filter_fn:
            candidates = [e for e in candidates if filter_fn(e)]

        scored = []
        for entry in candidates:
            if len(entry.vector) != len(query_vector):
                continue
            score = self._score(query_vector, entry.vector)
            if score >= min_score:
                scored.append((score, entry))

        scored.sort(key=lambda x: -x[0])
        hits = []
        for rank, (score, entry) in enumerate(scored[:top_k]):
            hits.append(SearchHit(entry=entry, score=score, rank=rank + 1))

        self._stats["hits"] += len(hits)
        return hits

    def search_by_id(self, vec_id: str, top_k: int = 10, **kwargs) -> list[SearchHit]:
        entry = self.get(vec_id)
        if not entry:
            return []
        hits = self.search(entry.vector, top_k + 1, **kwargs)
        return [h for h in hits if h.entry.vec_id != vec_id][:top_k]

    def count(self, namespace: str | None = None) -> int:
        if namespace:
            return sum(1 for e in self._entries.values() if e.namespace == namespace)
        return len(self._entries)

    def namespaces(self) -> list[str]:
        return list({e.namespace for e in self._entries.values()})

    def list_entries(
        self, namespace: str | None = None, limit: int = 100
    ) -> list[dict]:
        entries = list(self._entries.values())
        if namespace:
            entries = [e for e in entries if e.namespace == namespace]
        return [e.to_dict() for e in entries[:limit]]

    def stats(self) -> dict:
        return {
            **self._stats,
            "total": len(self._entries),
            "namespaces": self.namespaces(),
            "metric": self._metric,
        }


def build_jarvis_vector_store() -> VectorStore:
    return VectorStore(metric="cosine", persist=True)


async def main():
    import sys
    import random

    store = build_jarvis_vector_store()
    await store.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        random.seed(42)
        dim = 64

        # Insert some vectors
        print("Inserting 20 random vectors...")
        for i in range(20):
            vec = [random.gauss(0, 1) for _ in range(dim)]
            mag = math.sqrt(sum(x * x for x in vec))
            vec = [x / mag for x in vec]
            store.upsert(
                vec,
                payload={"label": f"item_{i}", "value": random.randint(0, 100)},
                namespace="test" if i < 10 else "other",
                tags=["even" if i % 2 == 0 else "odd"],
            )

        print(f"Total vectors: {store.count()}")
        print(f"Namespaces: {store.namespaces()}")

        # Search
        query = [random.gauss(0, 1) for _ in range(dim)]
        mag = math.sqrt(sum(x * x for x in query))
        query = [x / mag for x in query]

        hits = store.search(query, top_k=5, namespace="test")
        print("\nTop-5 results (namespace=test):")
        for h in hits:
            print(
                f"  [{h.rank}] {h.entry.payload.get('label'):<10} score={h.score:.4f}"
            )

        print(f"\nStats: {json.dumps(store.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(store.stats(), indent=2))

    elif cmd == "list":
        for e in store.list_entries(limit=20):
            print(f"  {e['vec_id']:<15} [{e['namespace']}] dim={e['dim']}")


if __name__ == "__main__":
    asyncio.run(main())
