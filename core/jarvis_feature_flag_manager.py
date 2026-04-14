#!/usr/bin/env python3
"""
jarvis_feature_flag_manager — Runtime feature flags with rollout percentages
Toggle features per user/model/environment without restarts, with audit trail
"""

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.feature_flag_manager")

REDIS_PREFIX = "jarvis:flags:"
FLAGS_FILE = Path("/home/turbo/IA/Core/jarvis/data/feature_flags.json")


class FlagState(str, Enum):
    ON = "on"
    OFF = "off"
    ROLLOUT = "rollout"  # percentage-based
    ALLOWLIST = "allowlist"


@dataclass
class FeatureFlag:
    name: str
    state: FlagState = FlagState.OFF
    rollout_pct: float = 0.0  # 0-100
    allowlist: list[str] = field(default_factory=list)
    denylist: list[str] = field(default_factory=list)
    description: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "state": self.state.value,
            "rollout_pct": self.rollout_pct,
            "allowlist": self.allowlist,
            "denylist": self.denylist,
            "description": self.description,
            "tags": self.tags,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FeatureFlag":
        d = dict(d)
        d["state"] = FlagState(d.get("state", "off"))
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def _bucket(entity: str, flag_name: str) -> float:
    """Deterministic 0-100 bucket for rollout."""
    h = hashlib.md5(f"{flag_name}:{entity}".encode()).hexdigest()
    return (int(h[:8], 16) % 10000) / 100.0


class FeatureFlagManager:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._flags: dict[str, FeatureFlag] = {}
        self._eval_cache: dict[
            str, tuple[bool, float]
        ] = {}  # (flag+entity) → (result, ts)
        self._cache_ttl_s = 5.0
        self._stats: dict[str, int] = {
            "evaluations": 0,
            "cache_hits": 0,
            "flag_changes": 0,
        }
        self._load()

    def _load(self):
        if FLAGS_FILE.exists():
            try:
                data = json.loads(FLAGS_FILE.read_text())
                for fd in data.get("flags", []):
                    flag = FeatureFlag.from_dict(fd)
                    self._flags[flag.name] = flag
                log.debug(f"Loaded {len(self._flags)} feature flags")
            except Exception as e:
                log.warning(f"Flag load error: {e}")

    def _save(self):
        FLAGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        FLAGS_FILE.write_text(
            json.dumps({"flags": [f.to_dict() for f in self._flags.values()]}, indent=2)
        )

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
            # Load from Redis (may be more recent than file)
            raw = await self.redis.hgetall(f"{REDIS_PREFIX}all")
            for name, blob in raw.items():
                flag = FeatureFlag.from_dict(json.loads(blob))
                self._flags[flag.name] = flag
        except Exception:
            self.redis = None

    def create(
        self,
        name: str,
        state: FlagState = FlagState.OFF,
        rollout_pct: float = 0.0,
        description: str = "",
        tags: list[str] | None = None,
    ) -> FeatureFlag:
        flag = FeatureFlag(
            name=name,
            state=state,
            rollout_pct=rollout_pct,
            description=description,
            tags=tags or [],
        )
        self._flags[name] = flag
        self._save()
        self._stats["flag_changes"] += 1
        if self.redis:
            asyncio.create_task(
                self.redis.hset(f"{REDIS_PREFIX}all", name, json.dumps(flag.to_dict()))
            )
        return flag

    def update(self, name: str, **kwargs) -> FeatureFlag | None:
        flag = self._flags.get(name)
        if not flag:
            return None
        for k, v in kwargs.items():
            if k == "state" and isinstance(v, str):
                v = FlagState(v)
            if hasattr(flag, k):
                setattr(flag, k, v)
        flag.updated_at = time.time()
        self._eval_cache.clear()  # invalidate cache
        self._save()
        self._stats["flag_changes"] += 1
        return flag

    def is_enabled(self, name: str, entity: str = "default") -> bool:
        """Evaluate flag for entity (user/model/session id)."""
        cache_key = f"{name}:{entity}"
        cached = self._eval_cache.get(cache_key)
        if cached and (time.time() - cached[1]) < self._cache_ttl_s:
            self._stats["cache_hits"] += 1
            return cached[0]

        self._stats["evaluations"] += 1
        flag = self._flags.get(name)
        if not flag:
            return False

        result = self._evaluate(flag, entity)
        self._eval_cache[cache_key] = (result, time.time())
        return result

    def _evaluate(self, flag: FeatureFlag, entity: str) -> bool:
        if flag.state == FlagState.ON:
            return entity not in flag.denylist
        if flag.state == FlagState.OFF:
            return False
        if flag.state == FlagState.ALLOWLIST:
            return entity in flag.allowlist and entity not in flag.denylist
        if flag.state == FlagState.ROLLOUT:
            if entity in flag.denylist:
                return False
            if entity in flag.allowlist:
                return True
            return _bucket(entity, flag.name) < flag.rollout_pct
        return False

    def get(self, name: str) -> FeatureFlag | None:
        return self._flags.get(name)

    def list_flags(self, tag: str | None = None) -> list[dict]:
        flags = list(self._flags.values())
        if tag:
            flags = [f for f in flags if tag in f.tags]
        return [f.to_dict() for f in flags]

    def enable(self, name: str):
        self.update(name, state=FlagState.ON)

    def disable(self, name: str):
        self.update(name, state=FlagState.OFF)

    def set_rollout(self, name: str, pct: float):
        self.update(name, state=FlagState.ROLLOUT, rollout_pct=pct)

    def stats(self) -> dict:
        on = sum(1 for f in self._flags.values() if f.state == FlagState.ON)
        return {
            **self._stats,
            "total_flags": len(self._flags),
            "on": on,
            "off": sum(1 for f in self._flags.values() if f.state == FlagState.OFF),
            "rollout": sum(
                1 for f in self._flags.values() if f.state == FlagState.ROLLOUT
            ),
            "cache_size": len(self._eval_cache),
        }


async def main():
    import sys

    mgr = FeatureFlagManager()
    await mgr.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        mgr.create(
            "new_inference_engine",
            FlagState.ROLLOUT,
            rollout_pct=30.0,
            description="New LLM routing engine",
            tags=["inference"],
        )
        mgr.create(
            "debug_mode", FlagState.ALLOWLIST, description="Debug output", tags=["dev"]
        )
        mgr.update("debug_mode", allowlist=["turbo", "admin"])
        mgr.create("stream_responses", FlagState.ON, description="SSE streaming")

        users = ["turbo", "alice", "bob", "carol", "dave", "eve", "frank"]
        print("Feature: new_inference_engine (30% rollout)")
        enabled = [u for u in users if mgr.is_enabled("new_inference_engine", u)]
        print(f"  Enabled for: {enabled} ({len(enabled)}/{len(users)})")

        print("\nFeature: debug_mode (allowlist: turbo, admin)")
        for u in ["turbo", "alice", "admin"]:
            print(f"  {u}: {mgr.is_enabled('debug_mode', u)}")

        print(f"\nStats: {json.dumps(mgr.stats(), indent=2)}")

    elif cmd == "list":
        for f in mgr.list_flags():
            print(f"  {f['name']:<30} {f['state']:<10} pct={f['rollout_pct']}")

    elif cmd == "stats":
        print(json.dumps(mgr.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
