#!/usr/bin/env python3
"""
jarvis_feature_store — ML feature storage and serving
Stores computed features (embeddings, scores, metadata) with TTL and versioning
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.feature_store")

REDIS_PREFIX = "jarvis:feat:"
STORE_FILE = Path("/home/turbo/IA/Core/jarvis/data/feature_store.jsonl")
DEFAULT_TTL_S = 86400  # 24h


@dataclass
class Feature:
    key: str
    namespace: str
    value: Any
    version: int = 1
    tags: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    ttl_s: int = DEFAULT_TTL_S

    @property
    def expires_at(self) -> float:
        return self.created_at + self.ttl_s if self.ttl_s else float("inf")

    @property
    def expired(self) -> bool:
        return time.time() > self.expires_at

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "namespace": self.namespace,
            "value": self.value,
            "version": self.version,
            "tags": self.tags,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "ttl_s": self.ttl_s,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Feature":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class FeatureGroup:
    """A set of related features for a single entity."""

    entity_id: str
    namespace: str
    features: dict[str, Any] = field(default_factory=dict)
    version: int = 1
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "entity_id": self.entity_id,
            "namespace": self.namespace,
            "features": self.features,
            "version": self.version,
            "ts": self.ts,
        }


class FeatureStore:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._local: dict[str, Feature] = {}  # namespace:key → Feature
        STORE_FILE.parent.mkdir(parents=True, exist_ok=True)

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def _full_key(self, namespace: str, key: str) -> str:
        return f"{namespace}:{key}"

    async def set(
        self,
        key: str,
        value: Any,
        namespace: str = "default",
        tags: list[str] | None = None,
        ttl_s: int = DEFAULT_TTL_S,
        version: int | None = None,
    ) -> Feature:
        fk = self._full_key(namespace, key)
        existing = self._local.get(fk)
        ver = version or ((existing.version + 1) if existing else 1)

        feature = Feature(
            key=key,
            namespace=namespace,
            value=value,
            version=ver,
            tags=tags or [],
            ttl_s=ttl_s,
            updated_at=time.time(),
            created_at=existing.created_at if existing else time.time(),
        )
        self._local[fk] = feature

        if self.redis:
            await self.redis.setex(
                f"{REDIS_PREFIX}{fk}",
                ttl_s or DEFAULT_TTL_S,
                json.dumps(feature.to_dict(), default=str),
            )
            # Tag index
            for tag in feature.tags:
                await self.redis.sadd(f"{REDIS_PREFIX}tag:{tag}", fk)

        log.debug(f"Feature set: {fk} v{ver}")
        return feature

    async def get(self, key: str, namespace: str = "default") -> Feature | None:
        fk = self._full_key(namespace, key)

        # Local cache
        local = self._local.get(fk)
        if local and not local.expired:
            return local

        # Redis
        if self.redis:
            raw = await self.redis.get(f"{REDIS_PREFIX}{fk}")
            if raw:
                feature = Feature.from_dict(json.loads(raw))
                self._local[fk] = feature
                return feature

        return None

    async def get_value(
        self, key: str, namespace: str = "default", default: Any = None
    ) -> Any:
        feature = await self.get(key, namespace)
        return feature.value if feature else default

    async def set_group(
        self,
        entity_id: str,
        namespace: str,
        features: dict[str, Any],
        ttl_s: int = DEFAULT_TTL_S,
    ):
        """Set multiple features for an entity atomically."""
        group = FeatureGroup(
            entity_id=entity_id, namespace=namespace, features=features
        )
        tasks = [
            self.set(f"{entity_id}:{k}", v, namespace=namespace, ttl_s=ttl_s)
            for k, v in features.items()
        ]
        await asyncio.gather(*tasks)
        if self.redis:
            await self.redis.setex(
                f"{REDIS_PREFIX}group:{namespace}:{entity_id}",
                ttl_s,
                json.dumps(group.to_dict(), default=str),
            )
        return group

    async def get_group(self, entity_id: str, namespace: str) -> FeatureGroup | None:
        if self.redis:
            raw = await self.redis.get(f"{REDIS_PREFIX}group:{namespace}:{entity_id}")
            if raw:
                return FeatureGroup(**json.loads(raw))
        return None

    async def get_by_tag(self, tag: str) -> list[Feature]:
        results = []
        if self.redis:
            keys = await self.redis.smembers(f"{REDIS_PREFIX}tag:{tag}")
            for fk in keys:
                raw = await self.redis.get(f"{REDIS_PREFIX}{fk}")
                if raw:
                    results.append(Feature.from_dict(json.loads(raw)))
        else:
            results = [
                f for f in self._local.values() if tag in f.tags and not f.expired
            ]
        return results

    async def delete(self, key: str, namespace: str = "default"):
        fk = self._full_key(namespace, key)
        self._local.pop(fk, None)
        if self.redis:
            await self.redis.delete(f"{REDIS_PREFIX}{fk}")

    async def list_keys(self, namespace: str = "default") -> list[str]:
        prefix = f"{namespace}:"
        local_keys = [
            fk[len(prefix) :]
            for fk in self._local
            if fk.startswith(prefix) and not self._local[fk].expired
        ]
        if self.redis:
            redis_keys = await self.redis.keys(f"{REDIS_PREFIX}{prefix}*")
            remote = [k.replace(f"{REDIS_PREFIX}{prefix}", "") for k in redis_keys]
            return list(set(local_keys + remote))
        return local_keys

    def stats(self) -> dict:
        active = [f for f in self._local.values() if not f.expired]
        namespaces = {f.namespace for f in active}
        return {
            "total_local": len(self._local),
            "active": len(active),
            "expired": len(self._local) - len(active),
            "namespaces": sorted(namespaces),
        }


async def main():
    import sys

    store = FeatureStore()
    await store.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        # Store model performance features
        await store.set(
            "qwen3.5-9b:latency_p50", 245.0, namespace="model_perf", tags=["latency"]
        )
        await store.set(
            "qwen3.5-9b:tok_per_s", 42.3, namespace="model_perf", tags=["throughput"]
        )
        await store.set(
            "qwen3.5-9b:error_rate", 0.02, namespace="model_perf", tags=["reliability"]
        )

        # Store user features
        await store.set_group(
            "user:turbo",
            "users",
            {
                "preferred_model": "qwen3.5-27b",
                "request_count": 1247,
                "avg_tokens": 512,
            },
        )

        # Retrieve
        latency = await store.get_value(
            "qwen3.5-9b:latency_p50", namespace="model_perf"
        )
        print(f"Model latency p50: {latency}ms")

        group = await store.get_group("user:turbo", "users")
        if group:
            print(f"User features: {group.features}")

        # Tag search
        latency_features = await store.get_by_tag("latency")
        print(f"Features tagged 'latency': {[f.key for f in latency_features]}")

        print(f"\nStats: {json.dumps(store.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(store.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
