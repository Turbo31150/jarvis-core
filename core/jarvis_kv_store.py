#!/usr/bin/env python3
"""
jarvis_kv_store — Tiered key-value store: in-memory L1 + Redis L2 + JSONL disk L3
TTL support, namespace isolation, LRU eviction, and atomic CAS operations
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.kv_store")

REDIS_PREFIX = "jarvis:kv:"
DISK_FILE = Path("/home/turbo/IA/Core/jarvis/data/kv_store.jsonl")
DEFAULT_TTL = 3600.0
MAX_L1_SIZE = 1000


@dataclass
class KVEntry:
    key: str
    value: Any
    namespace: str = "default"
    ttl_s: float = DEFAULT_TTL
    created_at: float = field(default_factory=time.time)
    accessed_at: float = field(default_factory=time.time)
    version: int = 1

    @property
    def expires_at(self) -> float:
        return self.created_at + self.ttl_s if self.ttl_s > 0 else float("inf")

    @property
    def expired(self) -> bool:
        return self.ttl_s > 0 and time.time() > self.expires_at

    @property
    def remaining_ttl(self) -> float:
        return max(0.0, self.expires_at - time.time())

    def to_dict(self, include_value: bool = True) -> dict:
        d: dict = {
            "key": self.key,
            "namespace": self.namespace,
            "ttl_s": self.ttl_s,
            "created_at": self.created_at,
            "version": self.version,
        }
        if include_value:
            d["value"] = self.value
        return d


class KVStore:
    """Tiered KV store: L1 (memory) → L2 (Redis) → L3 (disk)."""

    def __init__(
        self,
        max_l1: int = MAX_L1_SIZE,
        persist: bool = True,
        default_ttl: float = DEFAULT_TTL,
    ):
        self.redis: aioredis.Redis | None = None
        self._l1: dict[str, KVEntry] = {}  # key → entry
        self._access_order: list[str] = []  # LRU tracking
        self._max_l1 = max_l1
        self._persist = persist
        self._default_ttl = default_ttl
        self._stats: dict[str, int] = {
            "l1_hits": 0,
            "l2_hits": 0,
            "l3_hits": 0,
            "misses": 0,
            "sets": 0,
            "deletes": 0,
            "evictions": 0,
            "expirations": 0,
        }
        if persist:
            DISK_FILE.parent.mkdir(parents=True, exist_ok=True)
            self._load_l3()

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def _load_l3(self):
        if not DISK_FILE.exists():
            return
        try:
            for line in DISK_FILE.read_text().splitlines():
                if not line.strip():
                    continue
                d = json.loads(line)
                entry = KVEntry(
                    key=d["key"],
                    value=d.get("value"),
                    namespace=d.get("namespace", "default"),
                    ttl_s=d.get("ttl_s", DEFAULT_TTL),
                    created_at=d.get("created_at", time.time()),
                    version=d.get("version", 1),
                )
                if not entry.expired:
                    self._l1[entry.key] = entry
        except Exception as e:
            log.warning(f"KV L3 load error: {e}")

    def _ns_key(self, key: str, namespace: str) -> str:
        return f"{namespace}:{key}"

    def _evict_lru(self):
        """Evict least-recently used entries until under max_l1."""
        while len(self._l1) >= self._max_l1 and self._access_order:
            oldest = self._access_order.pop(0)
            if oldest in self._l1:
                del self._l1[oldest]
                self._stats["evictions"] += 1

    def _touch(self, full_key: str):
        if full_key in self._access_order:
            self._access_order.remove(full_key)
        self._access_order.append(full_key)

    def set(
        self,
        key: str,
        value: Any,
        namespace: str = "default",
        ttl_s: float | None = None,
        version: int | None = None,
    ) -> KVEntry:
        full_key = self._ns_key(key, namespace)
        ttl = ttl_s if ttl_s is not None else self._default_ttl

        existing = self._l1.get(full_key)
        ver = (existing.version + 1) if existing and version is None else (version or 1)

        entry = KVEntry(
            key=full_key,
            value=value,
            namespace=namespace,
            ttl_s=ttl,
            version=ver,
        )

        if len(self._l1) >= self._max_l1 and full_key not in self._l1:
            self._evict_lru()

        self._l1[full_key] = entry
        self._touch(full_key)
        self._stats["sets"] += 1

        # Async write to L2 and L3
        if self.redis:
            asyncio.create_task(self._write_l2(full_key, entry))
        if self._persist:
            self._write_l3(entry)

        return entry

    async def _write_l2(self, full_key: str, entry: KVEntry):
        try:
            redis_key = f"{REDIS_PREFIX}{full_key}"
            ttl_int = int(entry.ttl_s) if entry.ttl_s > 0 else 0
            serialized = json.dumps(entry.to_dict())
            if ttl_int > 0:
                await self.redis.setex(redis_key, ttl_int, serialized)
            else:
                await self.redis.set(redis_key, serialized)
        except Exception as e:
            log.warning(f"KV L2 write error: {e}")

    def _write_l3(self, entry: KVEntry):
        try:
            with open(DISK_FILE, "a") as f:
                f.write(json.dumps(entry.to_dict()) + "\n")
        except Exception:
            pass

    async def get(self, key: str, namespace: str = "default") -> Any | None:
        full_key = self._ns_key(key, namespace)

        # L1
        entry = self._l1.get(full_key)
        if entry:
            if entry.expired:
                del self._l1[full_key]
                self._stats["expirations"] += 1
            else:
                entry.accessed_at = time.time()
                self._touch(full_key)
                self._stats["l1_hits"] += 1
                return entry.value

        # L2 (Redis)
        if self.redis:
            try:
                raw = await self.redis.get(f"{REDIS_PREFIX}{full_key}")
                if raw:
                    d = json.loads(raw)
                    entry = KVEntry(
                        key=d["key"],
                        value=d["value"],
                        namespace=d.get("namespace", namespace),
                        ttl_s=d.get("ttl_s", DEFAULT_TTL),
                        created_at=d.get("created_at", time.time()),
                        version=d.get("version", 1),
                    )
                    if not entry.expired:
                        self._l1[full_key] = entry
                        self._touch(full_key)
                        self._stats["l2_hits"] += 1
                        return entry.value
            except Exception:
                pass

        self._stats["misses"] += 1
        return None

    def get_sync(self, key: str, namespace: str = "default") -> Any | None:
        """L1-only synchronous get."""
        full_key = self._ns_key(key, namespace)
        entry = self._l1.get(full_key)
        if entry and not entry.expired:
            self._stats["l1_hits"] += 1
            return entry.value
        return None

    async def delete(self, key: str, namespace: str = "default") -> bool:
        full_key = self._ns_key(key, namespace)
        existed = full_key in self._l1
        self._l1.pop(full_key, None)
        if full_key in self._access_order:
            self._access_order.remove(full_key)
        if self.redis:
            try:
                await self.redis.delete(f"{REDIS_PREFIX}{full_key}")
            except Exception:
                pass
        if existed:
            self._stats["deletes"] += 1
        return existed

    async def cas(
        self,
        key: str,
        expected_version: int,
        new_value: Any,
        namespace: str = "default",
    ) -> bool:
        """Compare-and-swap: only update if current version matches."""
        full_key = self._ns_key(key, namespace)
        entry = self._l1.get(full_key)
        if not entry or entry.expired:
            return False
        if entry.version != expected_version:
            return False
        self.set(key, new_value, namespace, entry.ttl_s, version=expected_version + 1)
        return True

    def keys(self, namespace: str | None = None) -> list[str]:
        result = []
        for full_key, entry in self._l1.items():
            if not entry.expired:
                if namespace is None or entry.namespace == namespace:
                    result.append(full_key)
        return result

    def purge_expired(self) -> int:
        expired = [k for k, e in self._l1.items() if e.expired]
        for k in expired:
            del self._l1[k]
            if k in self._access_order:
                self._access_order.remove(k)
        self._stats["expirations"] += len(expired)
        return len(expired)

    def stats(self) -> dict:
        total_gets = (
            self._stats["l1_hits"]
            + self._stats["l2_hits"]
            + self._stats["l3_hits"]
            + self._stats["misses"]
        )
        hit_rate = (total_gets - self._stats["misses"]) / max(total_gets, 1)
        return {
            **self._stats,
            "l1_size": len(self._l1),
            "hit_rate": round(hit_rate, 3),
        }


def build_jarvis_kv_store() -> KVStore:
    return KVStore(max_l1=1000, persist=True, default_ttl=3600.0)


async def main():
    import sys

    store = build_jarvis_kv_store()
    await store.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        # Basic set/get
        store.set(
            "model_list", ["qwen3.5-9b", "deepseek-r1"], namespace="cluster", ttl_s=60
        )
        store.set("gpu_temp_m1", 65.2, namespace="metrics", ttl_s=30)
        store.set("config_version", 42, namespace="config")

        v1 = await store.get("model_list", "cluster")
        v2 = await store.get("gpu_temp_m1", "metrics")
        v3 = await store.get("nonexistent", "default")
        print(f"model_list={v1}")
        print(f"gpu_temp_m1={v2}")
        print(f"nonexistent={v3}")

        # CAS
        ok = await store.cas("config_version", 1, 43, "config")
        print(f"\nCAS v1→43: {ok}")
        new_ver = await store.get("config_version", "config")
        print(f"config_version after CAS: {new_ver}")

        # TTL
        store.set("short_lived", "bye", ttl_s=0.01)
        await asyncio.sleep(0.02)
        expired_val = await store.get("short_lived")
        print(f"\nExpired key: {expired_val}")

        purged = store.purge_expired()
        print(f"Purged {purged} expired entries")
        print(f"\nKeys: {store.keys()}")
        print(f"\nStats: {json.dumps(store.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(store.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
