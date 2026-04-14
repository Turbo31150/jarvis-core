#!/usr/bin/env python3
"""
jarvis_agent_memory — Per-agent persistent memory with semantic recall
Stores facts, preferences, learned patterns per agent with vector-style search
"""

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.agent_memory")

MEMORY_DIR = Path("/home/turbo/IA/Core/jarvis/data/agent_memory")
REDIS_PREFIX = "jarvis:amem:"
DEFAULT_TTL = 0  # persistent by default


@dataclass
class MemoryEntry:
    entry_id: str
    agent: str
    content: str
    memory_type: str  # fact | preference | pattern | episode | skill
    importance: float = 0.5  # 0.0-1.0
    tags: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    access_count: int = 0
    source: str = ""  # where this memory came from

    def to_dict(self) -> dict:
        return {
            "entry_id": self.entry_id,
            "agent": self.agent,
            "content": self.content,
            "memory_type": self.memory_type,
            "importance": self.importance,
            "tags": self.tags,
            "created_at": self.created_at,
            "last_accessed": self.last_accessed,
            "access_count": self.access_count,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryEntry":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def relevance_score(self, query_terms: list[str]) -> float:
        """Simple keyword relevance + recency + importance."""
        content_lower = self.content.lower()
        tag_lower = [t.lower() for t in self.tags]

        keyword_hits = sum(
            1 for term in query_terms if term in content_lower or term in tag_lower
        )
        keyword_score = keyword_hits / max(len(query_terms), 1)

        # Recency decay: half-life 7 days
        age_days = (time.time() - self.last_accessed) / 86400
        recency = max(0.0, 1.0 - age_days / 14)

        return 0.5 * keyword_score + 0.3 * self.importance + 0.2 * recency


def _make_id(agent: str, content: str) -> str:
    return hashlib.sha256(f"{agent}:{content}".encode()).hexdigest()[:12]


class AgentMemory:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._cache: dict[str, dict[str, MemoryEntry]] = {}  # agent -> {id -> entry}
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def _agent_key(self, agent: str) -> str:
        return f"{REDIS_PREFIX}{agent}"

    async def store(
        self,
        agent: str,
        content: str,
        memory_type: str = "fact",
        importance: float = 0.5,
        tags: list[str] | None = None,
        source: str = "",
    ) -> MemoryEntry:
        entry_id = _make_id(agent, content)

        # Check if exists (deduplicate)
        existing = await self.get_by_id(agent, entry_id)
        if existing:
            existing.access_count += 1
            existing.last_accessed = time.time()
            existing.importance = max(existing.importance, importance)
            await self._save_entry(agent, existing)
            return existing

        entry = MemoryEntry(
            entry_id=entry_id,
            agent=agent,
            content=content,
            memory_type=memory_type,
            importance=importance,
            tags=tags or [],
            source=source,
        )
        await self._save_entry(agent, entry)
        log.debug(f"[{agent}] Stored {memory_type}: {content[:60]}")
        return entry

    async def _save_entry(self, agent: str, entry: MemoryEntry):
        if agent not in self._cache:
            self._cache[agent] = {}
        self._cache[agent][entry.entry_id] = entry

        if self.redis:
            await self.redis.hset(
                self._agent_key(agent),
                entry.entry_id,
                json.dumps(entry.to_dict()),
            )

        # Also persist to file
        path = MEMORY_DIR / f"{agent}.json"
        agent_data = {eid: e.to_dict() for eid, e in self._cache.get(agent, {}).items()}
        path.write_text(json.dumps(agent_data, indent=2))

    async def get_by_id(self, agent: str, entry_id: str) -> MemoryEntry | None:
        if agent in self._cache and entry_id in self._cache[agent]:
            return self._cache[agent][entry_id]
        if self.redis:
            raw = await self.redis.hget(self._agent_key(agent), entry_id)
            if raw:
                e = MemoryEntry.from_dict(json.loads(raw))
                if agent not in self._cache:
                    self._cache[agent] = {}
                self._cache[agent][entry_id] = e
                return e
        return None

    async def load_agent(self, agent: str) -> dict[str, MemoryEntry]:
        """Load all memories for an agent."""
        if agent in self._cache and self._cache[agent]:
            return self._cache[agent]

        # Try Redis
        if self.redis:
            raw_map = await self.redis.hgetall(self._agent_key(agent))
            if raw_map:
                self._cache[agent] = {
                    eid: MemoryEntry.from_dict(json.loads(raw))
                    for eid, raw in raw_map.items()
                }
                return self._cache[agent]

        # Try file
        path = MEMORY_DIR / f"{agent}.json"
        if path.exists():
            data = json.loads(path.read_text())
            self._cache[agent] = {
                eid: MemoryEntry.from_dict(d) for eid, d in data.items()
            }
        else:
            self._cache[agent] = {}

        return self._cache[agent]

    async def recall(
        self,
        agent: str,
        query: str,
        memory_type: str | None = None,
        top_k: int = 5,
        min_importance: float = 0.0,
    ) -> list[MemoryEntry]:
        """Recall most relevant memories for a query."""
        memories = await self.load_agent(agent)
        query_terms = query.lower().split()

        candidates = [
            e
            for e in memories.values()
            if e.importance >= min_importance
            and (memory_type is None or e.memory_type == memory_type)
        ]

        scored = sorted(
            candidates,
            key=lambda e: e.relevance_score(query_terms),
            reverse=True,
        )[:top_k]

        # Update access stats
        for e in scored:
            e.access_count += 1
            e.last_accessed = time.time()

        return scored

    async def forget(self, agent: str, entry_id: str) -> bool:
        memories = await self.load_agent(agent)
        if entry_id not in memories:
            return False
        del memories[entry_id]
        if self.redis:
            await self.redis.hdel(self._agent_key(agent), entry_id)
        # Update file
        path = MEMORY_DIR / f"{agent}.json"
        path.write_text(
            json.dumps({eid: e.to_dict() for eid, e in memories.items()}, indent=2)
        )
        return True

    async def stats(self, agent: str) -> dict:
        memories = await self.load_agent(agent)
        by_type: dict[str, int] = {}
        for e in memories.values():
            by_type[e.memory_type] = by_type.get(e.memory_type, 0) + 1

        return {
            "agent": agent,
            "total": len(memories),
            "by_type": by_type,
            "avg_importance": round(
                sum(e.importance for e in memories.values()) / max(len(memories), 1), 2
            ),
        }


# ── CLI ───────────────────────────────────────────────────────────────────────


async def main():
    import sys

    mem = AgentMemory()
    await mem.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"

    if cmd == "store" and len(sys.argv) > 3:
        agent = sys.argv[2]
        content = sys.argv[3]
        mtype = sys.argv[4] if len(sys.argv) > 4 else "fact"
        importance = float(sys.argv[5]) if len(sys.argv) > 5 else 0.5
        e = await mem.store(agent, content, memory_type=mtype, importance=importance)
        print(f"Stored [{e.entry_id}]: {e.content[:60]}")

    elif cmd == "recall" and len(sys.argv) > 3:
        agent = sys.argv[2]
        query = sys.argv[3]
        results = await mem.recall(agent, query)
        if not results:
            print("No memories found")
        for e in results:
            score = e.relevance_score(query.lower().split())
            print(
                f"  [{e.memory_type}] score={score:.2f} imp={e.importance:.1f}: {e.content[:80]}"
            )

    elif cmd == "stats" and len(sys.argv) > 2:
        s = await mem.stats(sys.argv[2])
        print(json.dumps(s, indent=2))

    elif cmd == "list" and len(sys.argv) > 2:
        agent = sys.argv[2]
        memories = await mem.load_agent(agent)
        print(f"{agent}: {len(memories)} memories")
        for e in sorted(memories.values(), key=lambda x: -x.importance)[:20]:
            print(
                f"  [{e.memory_type}] imp={e.importance:.1f} acc={e.access_count}: {e.content[:80]}"
            )

    elif cmd == "forget" and len(sys.argv) > 3:
        ok = await mem.forget(sys.argv[2], sys.argv[3])
        print(f"Forgot {sys.argv[3]}: {ok}")


if __name__ == "__main__":
    asyncio.run(main())
