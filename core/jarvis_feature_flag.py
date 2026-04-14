#!/usr/bin/env python3
"""
jarvis_feature_flag — Feature flag system with gradual rollout and targeting rules
Supports percentage rollout, user/entity targeting, kill switches, and Redis sync
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

log = logging.getLogger("jarvis.feature_flag")

REDIS_PREFIX = "jarvis:flags:"
FLAGS_FILE = Path("/home/turbo/IA/Core/jarvis/data/feature_flags.json")


class FlagState(str, Enum):
    ENABLED = "enabled"
    DISABLED = "disabled"
    ROLLOUT = "rollout"  # percentage-based
    TARGETED = "targeted"  # explicit allowlist


@dataclass
class TargetingRule:
    attribute: str  # e.g. "node", "model", "user_id"
    operator: str  # eq, neq, in, not_in, prefix
    value: str | list[str]  # comparison value(s)

    def matches(self, context: dict) -> bool:
        actual = context.get(self.attribute)
        if actual is None:
            return False
        if self.operator == "eq":
            return str(actual) == str(self.value)
        elif self.operator == "neq":
            return str(actual) != str(self.value)
        elif self.operator == "in":
            return str(actual) in [str(v) for v in self.value]
        elif self.operator == "not_in":
            return str(actual) not in [str(v) for v in self.value]
        elif self.operator == "prefix":
            return str(actual).startswith(str(self.value))
        return False


@dataclass
class FeatureFlag:
    key: str
    state: FlagState = FlagState.DISABLED
    rollout_pct: float = 0.0  # 0.0–100.0 for ROLLOUT state
    targeting_rules: list[TargetingRule] = field(default_factory=list)
    allowlist: list[str] = field(default_factory=list)  # entity ids always on
    denylist: list[str] = field(default_factory=list)  # entity ids always off
    description: str = ""
    owner: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    kill_switch: bool = False  # if True, overrides everything → disabled

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "state": self.state.value,
            "rollout_pct": self.rollout_pct,
            "description": self.description,
            "kill_switch": self.kill_switch,
            "updated_at": self.updated_at,
        }


def _bucket(entity_id: str, flag_key: str) -> float:
    """Deterministic 0–100 bucket for rollout via MD5."""
    h = hashlib.md5(f"{flag_key}:{entity_id}".encode()).hexdigest()
    return (int(h[:8], 16) % 10000) / 100.0


class FeatureFlagManager:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._flags: dict[str, FeatureFlag] = {}
        self._eval_cache: dict[
            str, tuple[bool, float]
        ] = {}  # key:entity → (result, ts)
        self._cache_ttl = 5.0
        self._stats: dict[str, int] = {
            "evaluations": 0,
            "cache_hits": 0,
            "enabled": 0,
            "disabled": 0,
        }
        FLAGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
            await self._sync_from_redis()
        except Exception:
            self.redis = None

    def _load(self):
        if not FLAGS_FILE.exists():
            return
        try:
            data = json.loads(FLAGS_FILE.read_text())
            for d in data.get("flags", []):
                rules = [
                    TargetingRule(r["attribute"], r["operator"], r["value"])
                    for r in d.get("targeting_rules", [])
                ]
                flag = FeatureFlag(
                    key=d["key"],
                    state=FlagState(d.get("state", "disabled")),
                    rollout_pct=d.get("rollout_pct", 0.0),
                    targeting_rules=rules,
                    allowlist=d.get("allowlist", []),
                    denylist=d.get("denylist", []),
                    description=d.get("description", ""),
                    owner=d.get("owner", ""),
                    kill_switch=d.get("kill_switch", False),
                )
                self._flags[flag.key] = flag
        except Exception as e:
            log.warning(f"Flag load error: {e}")

    def _save(self):
        try:
            data = {"flags": [f.to_dict() for f in self._flags.values()]}
            FLAGS_FILE.write_text(json.dumps(data, indent=2))
        except Exception as e:
            log.warning(f"Flag save error: {e}")

    async def _sync_from_redis(self):
        if not self.redis:
            return
        keys = await self.redis.keys(f"{REDIS_PREFIX}*")
        for key in keys:
            raw = await self.redis.get(key)
            if raw:
                try:
                    d = json.loads(raw)
                    flag_key = key.replace(REDIS_PREFIX, "")
                    if flag_key in self._flags:
                        self._flags[flag_key].state = FlagState(
                            d.get("state", "disabled")
                        )
                        self._flags[flag_key].rollout_pct = d.get("rollout_pct", 0.0)
                        self._flags[flag_key].kill_switch = d.get("kill_switch", False)
                except Exception:
                    pass

    def register(self, flag: FeatureFlag):
        self._flags[flag.key] = flag
        self._save()

    def set_state(self, key: str, state: FlagState, rollout_pct: float = 0.0):
        flag = self._flags.get(key)
        if not flag:
            log.warning(f"Flag not found: {key}")
            return
        flag.state = state
        flag.rollout_pct = rollout_pct
        flag.updated_at = time.time()
        self._eval_cache.clear()
        self._save()
        if self.redis:
            asyncio.create_task(
                self.redis.setex(
                    f"{REDIS_PREFIX}{key}",
                    3600,
                    json.dumps(flag.to_dict()),
                )
            )

    def kill(self, key: str):
        """Emergency kill switch — disables flag immediately."""
        flag = self._flags.get(key)
        if flag:
            flag.kill_switch = True
            flag.updated_at = time.time()
            self._eval_cache.clear()
            self._save()

    def is_enabled(
        self, key: str, entity_id: str = "", context: dict | None = None
    ) -> bool:
        self._stats["evaluations"] += 1
        ctx = context or {}

        # Cache check
        cache_key = f"{key}:{entity_id}"
        cached = self._eval_cache.get(cache_key)
        if cached and (time.time() - cached[1]) < self._cache_ttl:
            self._stats["cache_hits"] += 1
            return cached[0]

        flag = self._flags.get(key)
        if not flag:
            return False

        result = self._evaluate(flag, entity_id, ctx)
        self._eval_cache[cache_key] = (result, time.time())

        if result:
            self._stats["enabled"] += 1
        else:
            self._stats["disabled"] += 1
        return result

    def _evaluate(self, flag: FeatureFlag, entity_id: str, context: dict) -> bool:
        if flag.kill_switch:
            return False
        if flag.state == FlagState.DISABLED:
            return False
        if flag.state == FlagState.ENABLED:
            if entity_id and entity_id in flag.denylist:
                return False
            return True

        # Denylist always wins
        if entity_id and entity_id in flag.denylist:
            return False
        # Allowlist always on
        if entity_id and entity_id in flag.allowlist:
            return True

        if flag.state == FlagState.TARGETED:
            return any(rule.matches(context) for rule in flag.targeting_rules)

        if flag.state == FlagState.ROLLOUT:
            if not entity_id:
                return False
            bucket = _bucket(entity_id, flag.key)
            return bucket < flag.rollout_pct

        return False

    def get_flag(self, key: str) -> FeatureFlag | None:
        return self._flags.get(key)

    def list_flags(self) -> list[dict]:
        return [f.to_dict() for f in self._flags.values()]

    def stats(self) -> dict:
        return {**self._stats, "total_flags": len(self._flags)}


def build_jarvis_flags() -> FeatureFlagManager:
    mgr = FeatureFlagManager()

    defaults = [
        FeatureFlag(
            "streaming_responses",
            FlagState.ENABLED,
            description="SSE streaming for chat",
        ),
        FeatureFlag(
            "rag_retrieval",
            FlagState.ROLLOUT,
            rollout_pct=50.0,
            description="RAG on 50% of requests",
        ),
        FeatureFlag(
            "new_consensus_engine",
            FlagState.DISABLED,
            description="Experimental consensus v2",
        ),
        FeatureFlag(
            "gpu_scheduler_v2",
            FlagState.TARGETED,
            targeting_rules=[TargetingRule("node", "in", ["m1", "m2"])],
            description="New GPU scheduler for M1/M2",
        ),
        FeatureFlag(
            "model_caching",
            FlagState.ENABLED,
            description="KV cache for model responses",
        ),
    ]
    for flag in defaults:
        if flag.key not in mgr._flags:
            mgr.register(flag)

    return mgr


async def main():
    import sys

    mgr = build_jarvis_flags()
    await mgr.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Flag evaluations:")
        tests = [
            ("streaming_responses", "node_m1", {}),
            ("rag_retrieval", "req_001", {}),
            ("rag_retrieval", "req_002", {}),
            ("rag_retrieval", "req_003", {}),
            ("new_consensus_engine", "any", {}),
            ("gpu_scheduler_v2", "task_1", {"node": "m1"}),
            ("gpu_scheduler_v2", "task_2", {"node": "ol1"}),
        ]
        for key, entity, ctx in tests:
            result = mgr.is_enabled(key, entity, ctx)
            icon = "✅" if result else "❌"
            print(f"  {icon} {key:<30} entity={entity:<10} ctx={ctx}")

        # Kill switch demo
        mgr.kill("rag_retrieval")
        result = mgr.is_enabled("rag_retrieval", "req_001")
        print(
            f"\nAfter kill switch — rag_retrieval: {'✅' if result else '❌ (killed)'}"
        )

        print(f"\nStats: {json.dumps(mgr.stats(), indent=2)}")

    elif cmd == "list":
        for f in mgr.list_flags():
            print(f"  {f['key']:<35} {f['state']:<12} rollout={f['rollout_pct']}%")

    elif cmd == "stats":
        print(json.dumps(mgr.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
