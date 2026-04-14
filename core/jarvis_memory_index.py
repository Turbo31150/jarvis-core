#!/usr/bin/env python3
"""
jarvis_memory_index — Searchable index over agent memory entries
Full-text + semantic hybrid search, tagging, recency scoring, Redis-backed
"""

import asyncio
import hashlib
import json
import logging
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.memory_index")

INDEX_FILE = Path("/home/turbo/IA/Core/jarvis/data/memory_index.jsonl")
REDIS_PREFIX = "jarvis:memidx:"


class MemoryKind(str, Enum):
    FACT = "fact"
    EPISODE = "episode"
    PROCEDURE = "procedure"
    PREFERENCE = "preference"
    SKILL = "skill"
    CONTEXT = "context"


@dataclass
class MemoryEntry:
    entry_id: str
    content: str
    kind: MemoryKind = MemoryKind.FACT
    source: str = ""
    tags: list[str] = field(default_factory=list)
    embedding: list[float] = field(default_factory=list)
    importance: float = 1.0  # 0.0–1.0
    access_count: int = 0
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    expires_at: float = 0.0  # 0 = never
    metadata: dict = field(default_factory=dict)

    @property
    def expired(self) -> bool:
        return self.expires_at > 0 and time.time() > self.expires_at

    @property
    def age_s(self) -> float:
        return time.time() - self.created_at

    @property
    def recency_score(self) -> float:
        """Exponential decay: 1.0 at creation → ~0.37 after 1 day."""
        return math.exp(-self.age_s / 86400)

    def to_dict(self, include_embedding: bool = False) -> dict:
        d = {
            "entry_id": self.entry_id,
            "content": self.content[:500],
            "kind": self.kind.value,
            "source": self.source,
            "tags": self.tags,
            "importance": round(self.importance, 3),
            "access_count": self.access_count,
            "created_at": self.created_at,
            "last_accessed": self.last_accessed,
            "expires_at": self.expires_at,
            "metadata": self.metadata,
        }
        if include_embedding and self.embedding:
            d["embedding"] = self.embedding
        return d


@dataclass
class SearchResult:
    entry: MemoryEntry
    score: float
    match_type: str  # "exact", "keyword", "semantic", "tag"

    def to_dict(self) -> dict:
        return {
            **self.entry.to_dict(),
            "score": round(self.score, 4),
            "match_type": self.match_type,
        }


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    ma = math.sqrt(sum(x * x for x in a))
    mb = math.sqrt(sum(x * x for x in b))
    return dot / max(ma * mb, 1e-9)


def _keyword_score(content: str, query: str) -> float:
    words = query.lower().split()
    if not words:
        return 0.0
    hits = sum(1 for w in words if w in content.lower())
    return hits / len(words)


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:16]


class MemoryIndex:
    def __init__(
        self,
        max_size: int = 10_000,
        semantic_threshold: float = 0.82,
        persist: bool = True,
    ):
        self.redis: aioredis.Redis | None = None
        self._entries: dict[str, MemoryEntry] = {}  # entry_id → entry
        self._by_tag: dict[str, set[str]] = {}  # tag → set of entry_ids
        self._max_size = max_size
        self._semantic_threshold = semantic_threshold
        self._persist = persist
        self._stats: dict[str, Any] = {
            "added": 0,
            "searched": 0,
            "evictions": 0,
            "hits": 0,
        }
        if persist:
            INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
            self._load()

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def _load(self):
        if not INDEX_FILE.exists():
            return
        try:
            count = 0
            for line in INDEX_FILE.read_text().splitlines():
                if not line.strip():
                    continue
                d = json.loads(line)
                entry = MemoryEntry(
                    entry_id=d["entry_id"],
                    content=d["content"],
                    kind=MemoryKind(d.get("kind", "fact")),
                    source=d.get("source", ""),
                    tags=d.get("tags", []),
                    embedding=d.get("embedding", []),
                    importance=d.get("importance", 1.0),
                    access_count=d.get("access_count", 0),
                    created_at=d.get("created_at", time.time()),
                    last_accessed=d.get("last_accessed", time.time()),
                    expires_at=d.get("expires_at", 0.0),
                    metadata=d.get("metadata", {}),
                )
                if not entry.expired:
                    self._index_entry(entry)
                    count += 1
                if count >= self._max_size:
                    break
            log.info(f"Memory index loaded {count} entries")
        except Exception as e:
            log.warning(f"Memory index load error: {e}")

    def _index_entry(self, entry: MemoryEntry):
        self._entries[entry.entry_id] = entry
        for tag in entry.tags:
            self._by_tag.setdefault(tag.lower(), set()).add(entry.entry_id)

    def _deindex_entry(self, entry: MemoryEntry):
        self._entries.pop(entry.entry_id, None)
        for tag in entry.tags:
            s = self._by_tag.get(tag.lower(), set())
            s.discard(entry.entry_id)

    def add(self, entry: MemoryEntry) -> str:
        # Evict if at capacity: remove lowest importance + recency
        if len(self._entries) >= self._max_size:
            victim = min(
                self._entries.values(), key=lambda e: e.importance * e.recency_score
            )
            self._deindex_entry(victim)
            self._stats["evictions"] += 1

        self._index_entry(entry)
        self._stats["added"] += 1

        if self._persist:
            asyncio.create_task(self._persist_entry(entry))

        if self.redis:
            asyncio.create_task(self._redis_set(entry))

        return entry.entry_id

    def add_text(
        self,
        content: str,
        kind: MemoryKind = MemoryKind.FACT,
        source: str = "",
        tags: list[str] | None = None,
        importance: float = 1.0,
        embedding: list[float] | None = None,
        ttl_s: float = 0.0,
    ) -> MemoryEntry:
        entry_id = _content_hash(content)
        expires_at = time.time() + ttl_s if ttl_s > 0 else 0.0
        entry = MemoryEntry(
            entry_id=entry_id,
            content=content,
            kind=kind,
            source=source,
            tags=tags or [],
            embedding=embedding or [],
            importance=importance,
            expires_at=expires_at,
        )
        self.add(entry)
        return entry

    def search(
        self,
        query: str,
        query_embedding: list[float] | None = None,
        kinds: list[MemoryKind] | None = None,
        tags: list[str] | None = None,
        top_k: int = 10,
        min_score: float = 0.1,
    ) -> list[SearchResult]:
        self._stats["searched"] += 1
        candidates = list(self._entries.values())

        # Filter expired
        candidates = [e for e in candidates if not e.expired]

        # Filter by kind
        if kinds:
            candidates = [e for e in candidates if e.kind in kinds]

        # Filter by tags
        if tags:
            tag_ids: set[str] = set()
            for tag in tags:
                tag_ids.update(self._by_tag.get(tag.lower(), set()))
            candidates = [e for e in candidates if e.entry_id in tag_ids]

        results: list[SearchResult] = []

        for entry in candidates:
            # Keyword score
            kw = _keyword_score(entry.content, query)

            # Semantic score
            sem = 0.0
            if query_embedding and entry.embedding:
                sem = _cosine(query_embedding, entry.embedding)

            # Composite: weight by importance and recency
            raw_score = max(kw * 0.6, sem * 0.8)
            if raw_score < min_score:
                continue

            final_score = raw_score * (
                0.5 + 0.3 * entry.importance + 0.2 * entry.recency_score
            )
            match_type = "semantic" if sem > kw else "keyword"

            results.append(SearchResult(entry, final_score, match_type))

        results.sort(key=lambda r: -r.score)
        top = results[:top_k]

        # Update access tracking
        for r in top:
            r.entry.access_count += 1
            r.entry.last_accessed = time.time()
            self._stats["hits"] += 1

        return top

    def get(self, entry_id: str) -> MemoryEntry | None:
        entry = self._entries.get(entry_id)
        if entry and entry.expired:
            self._deindex_entry(entry)
            return None
        if entry:
            entry.access_count += 1
            entry.last_accessed = time.time()
        return entry

    def delete(self, entry_id: str) -> bool:
        entry = self._entries.get(entry_id)
        if not entry:
            return False
        self._deindex_entry(entry)
        return True

    def purge_expired(self) -> int:
        expired = [e for e in self._entries.values() if e.expired]
        for e in expired:
            self._deindex_entry(e)
        return len(expired)

    async def _persist_entry(self, entry: MemoryEntry):
        try:
            with open(INDEX_FILE, "a") as f:
                f.write(json.dumps(entry.to_dict(include_embedding=True)) + "\n")
        except Exception:
            pass

    async def _redis_set(self, entry: MemoryEntry):
        if not self.redis:
            return
        try:
            ttl = int(entry.expires_at - time.time()) if entry.expires_at > 0 else 86400
            await self.redis.setex(
                f"{REDIS_PREFIX}{entry.entry_id}",
                max(ttl, 1),
                json.dumps(entry.to_dict()),
            )
        except Exception:
            pass

    def stats(self) -> dict:
        kinds_dist: dict[str, int] = {}
        for e in self._entries.values():
            kinds_dist[e.kind.value] = kinds_dist.get(e.kind.value, 0) + 1
        return {
            **self._stats,
            "total_entries": len(self._entries),
            "unique_tags": len(self._by_tag),
            "kinds": kinds_dist,
        }


def build_jarvis_memory_index() -> MemoryIndex:
    return MemoryIndex(
        max_size=10_000,
        semantic_threshold=0.82,
        persist=True,
    )


async def main():
    import sys

    idx = build_jarvis_memory_index()
    await idx.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        # Add some entries
        idx.add_text(
            "Redis is an in-memory data store",
            MemoryKind.FACT,
            tags=["redis", "database"],
            importance=0.9,
        )
        idx.add_text(
            "JARVIS cluster has 3 GPU nodes: M1, M2, OL1",
            MemoryKind.FACT,
            tags=["cluster", "gpu"],
            importance=1.0,
        )
        idx.add_text(
            "Use lm-ask.sh for local LLM queries",
            MemoryKind.PROCEDURE,
            tags=["llm", "tools"],
            importance=0.8,
        )
        idx.add_text(
            "User prefers terse responses without preambles",
            MemoryKind.PREFERENCE,
            tags=["user", "style"],
            importance=0.95,
        )
        idx.add_text(
            "deepseek-r1 is best for reasoning tasks",
            MemoryKind.SKILL,
            tags=["model", "llm"],
            importance=0.85,
        )

        print(f"Index size: {len(idx._entries)} entries")

        queries = ["GPU cluster nodes", "LLM model selection", "user preferences"]
        for q in queries:
            results = idx.search(q, top_k=3, min_score=0.05)
            print(f"\nQuery: '{q}'")
            for r in results:
                print(
                    f"  [{r.match_type:<9}] score={r.score:.3f} "
                    f"kind={r.entry.kind.value:<12} {r.entry.content[:60]}"
                )

        print(f"\nStats: {json.dumps(idx.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(idx.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
