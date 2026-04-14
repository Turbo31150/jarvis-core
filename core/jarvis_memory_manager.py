#!/usr/bin/env python3
"""
jarvis_memory_manager — Hierarchical agent memory management
Working memory, episodic memory, semantic memory, forgetting curves, consolidation
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

log = logging.getLogger("jarvis.memory_manager")

REDIS_PREFIX = "jarvis:memory:"


class MemoryTier(str, Enum):
    WORKING = "working"  # current context, ~7 items, fast decay
    EPISODIC = "episodic"  # recent events, session-scoped
    SEMANTIC = "semantic"  # long-term facts, slow decay
    PROCEDURAL = "procedural"  # how-to knowledge, near-permanent


class MemoryTag(str, Enum):
    FACT = "fact"
    EVENT = "event"
    PREFERENCE = "preference"
    SKILL = "skill"
    ENTITY = "entity"
    RELATION = "relation"
    GOAL = "goal"
    ERROR = "error"


@dataclass
class MemoryItem:
    memory_id: str
    content: str
    tier: MemoryTier
    tags: list[MemoryTag] = field(default_factory=list)
    importance: float = 0.5  # 0.0 - 1.0
    confidence: float = 1.0
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    access_count: int = 0
    decay_rate: float = 0.0  # per-hour forgetting rate
    source: str = ""  # where this memory came from
    agent_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def touch(self):
        self.last_accessed = time.time()
        self.access_count += 1

    def strength(self) -> float:
        """Ebbinghaus forgetting curve: S = importance * e^(-decay * time)."""
        if self.decay_rate <= 0:
            return self.importance
        age_hours = (time.time() - self.created_at) / 3600
        recency_bonus = 0.1 * self.access_count
        raw = self.importance * math.exp(-self.decay_rate * age_hours)
        return min(1.0, raw + recency_bonus * 0.05)

    def is_forgotten(self, threshold: float = 0.05) -> bool:
        return self.strength() < threshold

    def to_dict(self) -> dict:
        return {
            "memory_id": self.memory_id,
            "content": self.content[:200],
            "tier": self.tier.value,
            "tags": [t.value for t in self.tags],
            "importance": round(self.importance, 3),
            "confidence": round(self.confidence, 3),
            "strength": round(self.strength(), 3),
            "access_count": self.access_count,
            "age_h": round((time.time() - self.created_at) / 3600, 2),
            "source": self.source,
        }


@dataclass
class ConsolidationResult:
    merged: int
    forgotten: int
    promoted: int
    duration_ms: float


class MemoryManager:
    """
    Hierarchical memory system with forgetting curves and consolidation.
    Working → Episodic → Semantic promotion based on strength and access count.
    """

    # Tier config: (capacity, decay_rate_per_hour, promote_threshold)
    _TIER_CONFIG = {
        MemoryTier.WORKING: (12, 0.5, 0.6),
        MemoryTier.EPISODIC: (500, 0.05, 0.4),
        MemoryTier.SEMANTIC: (10000, 0.002, None),
        MemoryTier.PROCEDURAL: (2000, 0.0, None),
    }

    def __init__(self, agent_id: str = "default"):
        self.redis: aioredis.Redis | None = None
        self.agent_id = agent_id
        self._memories: dict[str, MemoryItem] = {}
        self._stats: dict[str, int] = {
            "stored": 0,
            "recalled": 0,
            "forgotten": 0,
            "promoted": 0,
            "consolidated": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def _new_id(self) -> str:
        import secrets

        return secrets.token_hex(10)

    def store(
        self,
        content: str,
        tier: MemoryTier = MemoryTier.WORKING,
        tags: list[MemoryTag] | None = None,
        importance: float = 0.5,
        confidence: float = 1.0,
        source: str = "",
        metadata: dict | None = None,
    ) -> MemoryItem:
        # Determine decay rate from tier config
        _, decay_rate, _ = self._TIER_CONFIG[tier]

        item = MemoryItem(
            memory_id=self._new_id(),
            content=content,
            tier=tier,
            tags=tags or [],
            importance=importance,
            confidence=confidence,
            decay_rate=decay_rate,
            source=source,
            agent_id=self.agent_id,
            metadata=metadata or {},
        )
        self._memories[item.memory_id] = item
        self._stats["stored"] += 1

        # Enforce working memory capacity
        if tier == MemoryTier.WORKING:
            self._enforce_capacity(MemoryTier.WORKING)

        return item

    def _enforce_capacity(self, tier: MemoryTier):
        capacity, _, _ = self._TIER_CONFIG[tier]
        items = [m for m in self._memories.values() if m.tier == tier]
        if len(items) > capacity:
            # Evict weakest
            items.sort(key=lambda m: m.strength())
            for item in items[: len(items) - capacity]:
                del self._memories[item.memory_id]
                self._stats["forgotten"] += 1

    def recall(
        self,
        query: str = "",
        tier: MemoryTier | None = None,
        tags: list[MemoryTag] | None = None,
        top_k: int = 10,
        min_strength: float = 0.1,
    ) -> list[MemoryItem]:
        results = []
        q_words = set(query.lower().split()) if query else set()

        for item in self._memories.values():
            if item.strength() < min_strength:
                continue
            if tier and item.tier != tier:
                continue
            if tags and not any(t in item.tags for t in tags):
                continue

            score = item.strength()
            if q_words:
                content_words = set(item.content.lower().split())
                overlap = len(q_words & content_words) / max(len(q_words), 1)
                score = 0.4 * score + 0.6 * overlap

            results.append((score, item))

        results.sort(key=lambda x: -x[0])
        items = [item for _, item in results[:top_k]]
        for item in items:
            item.touch()
            self._stats["recalled"] += 1
        return items

    def get(self, memory_id: str) -> MemoryItem | None:
        item = self._memories.get(memory_id)
        if item:
            item.touch()
            self._stats["recalled"] += 1
        return item

    def forget(self, memory_id: str) -> bool:
        if memory_id in self._memories:
            del self._memories[memory_id]
            self._stats["forgotten"] += 1
            return True
        return False

    def reinforce(self, memory_id: str, importance_boost: float = 0.1):
        item = self._memories.get(memory_id)
        if item:
            item.importance = min(1.0, item.importance + importance_boost)
            item.created_at = time.time()  # reset decay clock
            item.touch()

    def consolidate(self) -> ConsolidationResult:
        """
        Housekeeping: forget weak memories, promote strong ones.
        Working → Episodic → Semantic based on strength thresholds.
        """
        t0 = time.time()
        forgotten = 0
        promoted = 0

        all_items = list(self._memories.values())

        for item in all_items:
            strength = item.strength()

            # Forget completely decayed items
            if item.decay_rate > 0 and item.is_forgotten():
                del self._memories[item.memory_id]
                forgotten += 1
                self._stats["forgotten"] += 1
                continue

            # Promote if strong enough
            _, _, promote_threshold = self._TIER_CONFIG[item.tier]
            if promote_threshold and strength >= promote_threshold:
                if item.tier == MemoryTier.WORKING:
                    item.tier = MemoryTier.EPISODIC
                    _, decay_rate, _ = self._TIER_CONFIG[MemoryTier.EPISODIC]
                    item.decay_rate = decay_rate
                    item.created_at = time.time()
                    promoted += 1
                    self._stats["promoted"] += 1
                elif item.tier == MemoryTier.EPISODIC and item.access_count >= 3:
                    item.tier = MemoryTier.SEMANTIC
                    _, decay_rate, _ = self._TIER_CONFIG[MemoryTier.SEMANTIC]
                    item.decay_rate = decay_rate
                    item.created_at = time.time()
                    promoted += 1
                    self._stats["promoted"] += 1

        # Dedup: merge exact duplicates within same tier
        merged = self._dedup_memories()
        self._stats["consolidated"] += 1

        return ConsolidationResult(
            merged=merged,
            forgotten=forgotten,
            promoted=promoted,
            duration_ms=(time.time() - t0) * 1000,
        )

    def _dedup_memories(self) -> int:
        seen: dict[str, str] = {}  # normalized_content -> memory_id
        to_delete = []
        for mid, item in self._memories.items():
            key = item.content.strip().lower()[:100]
            if key in seen:
                # Keep the stronger one
                existing = self._memories[seen[key]]
                if item.strength() > existing.strength():
                    to_delete.append(seen[key])
                    seen[key] = mid
                else:
                    to_delete.append(mid)
            else:
                seen[key] = mid
        for mid in to_delete:
            self._memories.pop(mid, None)
        return len(to_delete)

    def tier_summary(self) -> dict[str, dict]:
        summary: dict[str, dict] = {}
        for tier in MemoryTier:
            items = [m for m in self._memories.values() if m.tier == tier]
            capacity, _, _ = self._TIER_CONFIG[tier]
            summary[tier.value] = {
                "count": len(items),
                "capacity": capacity,
                "avg_strength": round(
                    sum(m.strength() for m in items) / max(len(items), 1), 3
                ),
            }
        return summary

    async def persist(self):
        if not self.redis:
            return
        try:
            key = f"{REDIS_PREFIX}{self.agent_id}"
            data = {
                mid: m.to_dict()
                for mid, m in self._memories.items()
                if m.tier in (MemoryTier.SEMANTIC, MemoryTier.PROCEDURAL)
            }
            await self.redis.setex(key, 86400 * 7, json.dumps(data))
        except Exception:
            pass

    def stats(self) -> dict:
        return {
            **self._stats,
            "total_memories": len(self._memories),
            "tiers": self.tier_summary(),
        }


def build_jarvis_memory_manager(agent_id: str = "jarvis") -> MemoryManager:
    return MemoryManager(agent_id=agent_id)


async def main():
    import sys

    mgr = build_jarvis_memory_manager()
    await mgr.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Memory manager demo...\n")

        # Store memories at different tiers
        mgr.store(
            "User prefers concise responses",
            MemoryTier.SEMANTIC,
            [MemoryTag.PREFERENCE],
            importance=0.9,
        )
        mgr.store(
            "Cluster M1 is primary inference node",
            MemoryTier.SEMANTIC,
            [MemoryTag.FACT],
            importance=0.85,
        )
        mgr.store(
            "User asked about GPU temps at 14:23",
            MemoryTier.EPISODIC,
            [MemoryTag.EVENT],
            importance=0.4,
        )
        mgr.store(
            "How to restart a service: systemctl restart X",
            MemoryTier.PROCEDURAL,
            [MemoryTag.SKILL],
            importance=0.95,
        )

        # Working memory
        for i in range(5):
            mgr.store(f"Working item {i}", MemoryTier.WORKING, importance=0.3 + i * 0.1)

        # Recall
        results = mgr.recall("GPU cluster node", top_k=3)
        print("  Recall 'GPU cluster node':")
        for m in results:
            print(f"    [{m.tier.value:10}] str={m.strength():.2f} {m.content[:60]}")

        # Consolidate
        result = mgr.consolidate()
        print(
            f"\n  Consolidation: merged={result.merged} forgotten={result.forgotten} "
            f"promoted={result.promoted}"
        )

        print("\n  Tier summary:")
        for tier, info in mgr.tier_summary().items():
            print(
                f"    {tier:<12} count={info['count']}/{info['capacity']} "
                f"avg_strength={info['avg_strength']}"
            )

        print(f"\nStats: {json.dumps(mgr.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(mgr.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
