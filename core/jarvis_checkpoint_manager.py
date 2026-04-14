#!/usr/bin/env python3
"""
jarvis_checkpoint_manager — State checkpointing and crash recovery
Periodic snapshots, incremental diffs, rollback to any checkpoint
"""

import asyncio
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.checkpoint_manager")

CHECKPOINT_DIR = Path("/home/turbo/IA/Core/jarvis/data/checkpoints")
REDIS_PREFIX = "jarvis:ckpt:"


class CheckpointKind(str, Enum):
    FULL = "full"  # complete state snapshot
    INCREMENTAL = "incr"  # only changed keys since last full
    AUTO = "auto"  # triggered automatically
    MANUAL = "manual"  # user-triggered


@dataclass
class Checkpoint:
    ckpt_id: str
    name: str
    kind: CheckpointKind
    state: dict  # the actual state data
    base_id: str = ""  # for incremental: parent checkpoint
    checksum: str = ""  # MD5 of serialized state
    size_bytes: int = 0
    ts: float = field(default_factory=time.time)
    tags: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def to_dict(self, include_state: bool = True) -> dict:
        d = {
            "ckpt_id": self.ckpt_id,
            "name": self.name,
            "kind": self.kind.value,
            "base_id": self.base_id,
            "checksum": self.checksum,
            "size_bytes": self.size_bytes,
            "ts": self.ts,
            "tags": self.tags,
            "metadata": self.metadata,
        }
        if include_state:
            d["state"] = self.state
        return d


def _compute_checksum(state: dict) -> str:
    serialized = json.dumps(state, sort_keys=True, default=str)
    return hashlib.md5(serialized.encode()).hexdigest()


def _state_diff(base: dict, current: dict) -> dict:
    """Compute keys that changed/added/deleted vs base."""
    diff = {}
    all_keys = set(base.keys()) | set(current.keys())
    for key in all_keys:
        base_val = base.get(key)
        curr_val = current.get(key)
        if base_val != curr_val:
            diff[key] = curr_val  # None means deleted
    return diff


def _apply_diff(base: dict, diff: dict) -> dict:
    """Reconstruct state from base + incremental diff."""
    result = dict(base)
    for key, val in diff.items():
        if val is None:
            result.pop(key, None)
        else:
            result[key] = val
    return result


class CheckpointManager:
    def __init__(
        self,
        max_checkpoints: int = 50,
        auto_interval_s: float = 300.0,
        incremental_after: int = 5,  # full checkpoint every N incrementals
        compress: bool = False,
    ):
        self.redis: aioredis.Redis | None = None
        self._checkpoints: dict[str, Checkpoint] = {}
        self._ordered: list[str] = []  # ordered list of ckpt_ids by ts
        self._max_checkpoints = max_checkpoints
        self._auto_interval_s = auto_interval_s
        self._incremental_after = incremental_after
        self._last_full_id: str | None = None
        self._incr_since_full: int = 0
        self._auto_task: asyncio.Task | None = None
        self._stats: dict[str, int] = {
            "saves": 0,
            "loads": 0,
            "rollbacks": 0,
            "evictions": 0,
        }
        CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
        self._load_index()

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def _load_index(self):
        """Load existing checkpoints from disk."""
        for path in sorted(CHECKPOINT_DIR.glob("*.json")):
            try:
                data = json.loads(path.read_text())
                ckpt = Checkpoint(
                    ckpt_id=data["ckpt_id"],
                    name=data["name"],
                    kind=CheckpointKind(data["kind"]),
                    state=data.get("state", {}),
                    base_id=data.get("base_id", ""),
                    checksum=data.get("checksum", ""),
                    size_bytes=data.get("size_bytes", 0),
                    ts=data.get("ts", 0),
                    tags=data.get("tags", []),
                    metadata=data.get("metadata", {}),
                )
                self._checkpoints[ckpt.ckpt_id] = ckpt
                self._ordered.append(ckpt.ckpt_id)
                if ckpt.kind == CheckpointKind.FULL:
                    self._last_full_id = ckpt.ckpt_id
            except Exception as e:
                log.warning(f"Failed to load checkpoint {path}: {e}")
        self._ordered.sort(key=lambda cid: self._checkpoints[cid].ts)
        log.info(f"Checkpoint index loaded: {len(self._checkpoints)} checkpoints")

    def save(
        self,
        state: dict,
        name: str = "",
        tags: list[str] | None = None,
        metadata: dict | None = None,
        force_full: bool = False,
    ) -> Checkpoint:
        ckpt_id = str(uuid.uuid4())[:12]
        checksum = _compute_checksum(state)

        # Skip if state hasn't changed since last checkpoint
        if self._ordered:
            last = self._checkpoints.get(self._ordered[-1])
            if last and last.checksum == checksum:
                log.debug("No state change — skipping checkpoint")
                return last

        # Decide full vs incremental
        use_incremental = (
            not force_full
            and self._last_full_id is not None
            and self._incr_since_full < self._incremental_after
        )

        if use_incremental:
            base = self._checkpoints[self._last_full_id]
            diff_state = _state_diff(base.state, state)
            kind = CheckpointKind.INCREMENTAL
            save_state = diff_state
            base_id = self._last_full_id
            self._incr_since_full += 1
        else:
            kind = CheckpointKind.FULL
            save_state = state
            base_id = ""
            self._last_full_id = ckpt_id
            self._incr_since_full = 0

        serialized = json.dumps(save_state, default=str)
        ckpt = Checkpoint(
            ckpt_id=ckpt_id,
            name=name or f"auto-{ckpt_id}",
            kind=kind,
            state=save_state,
            base_id=base_id,
            checksum=checksum,
            size_bytes=len(serialized.encode()),
            tags=tags or [],
            metadata=metadata or {},
        )

        self._checkpoints[ckpt_id] = ckpt
        self._ordered.append(ckpt_id)
        self._stats["saves"] += 1

        # Evict oldest if over limit
        while len(self._checkpoints) > self._max_checkpoints:
            oldest_id = self._ordered.pop(0)
            removed = self._checkpoints.pop(oldest_id, None)
            self._stats["evictions"] += 1
            path = CHECKPOINT_DIR / f"{oldest_id}.json"
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass

        # Persist to disk
        path = CHECKPOINT_DIR / f"{ckpt_id}.json"
        try:
            path.write_text(json.dumps(ckpt.to_dict()))
        except Exception as e:
            log.warning(f"Checkpoint disk write failed: {e}")

        if self.redis:
            asyncio.create_task(self._redis_save(ckpt))

        log.info(f"Checkpoint saved: {ckpt_id} ({kind.value}, {ckpt.size_bytes}B)")
        return ckpt

    def load(self, ckpt_id: str) -> dict | None:
        ckpt = self._checkpoints.get(ckpt_id)
        if not ckpt:
            return None

        self._stats["loads"] += 1

        if ckpt.kind == CheckpointKind.FULL:
            return dict(ckpt.state)

        # Reconstruct from base
        base = self._checkpoints.get(ckpt.base_id)
        if not base:
            log.warning(
                f"Base checkpoint {ckpt.base_id} not found for incremental {ckpt_id}"
            )
            return None

        return _apply_diff(base.state, ckpt.state)

    def rollback_to(self, ckpt_id: str) -> dict | None:
        state = self.load(ckpt_id)
        if state is None:
            return None
        self._stats["rollbacks"] += 1
        log.info(f"Rolled back to checkpoint {ckpt_id}")
        return state

    def latest(self) -> Checkpoint | None:
        if not self._ordered:
            return None
        return self._checkpoints.get(self._ordered[-1])

    def start_auto(self, state_provider: Any):
        """Start background auto-checkpoint using state_provider() → dict."""

        async def _loop():
            while True:
                await asyncio.sleep(self._auto_interval_s)
                try:
                    state = (
                        state_provider() if callable(state_provider) else state_provider
                    )
                    self.save(state, tags=["auto"])
                except Exception as e:
                    log.warning(f"Auto-checkpoint error: {e}")

        self._auto_task = asyncio.create_task(_loop())

    def stop_auto(self):
        if self._auto_task:
            self._auto_task.cancel()
            self._auto_task = None

    async def _redis_save(self, ckpt: Checkpoint):
        if not self.redis:
            return
        try:
            await self.redis.setex(
                f"{REDIS_PREFIX}{ckpt.ckpt_id}",
                3600,
                json.dumps(ckpt.to_dict(include_state=False)),
            )
            await self.redis.lpush(f"{REDIS_PREFIX}index", ckpt.ckpt_id)
            await self.redis.ltrim(f"{REDIS_PREFIX}index", 0, self._max_checkpoints - 1)
        except Exception as e:
            log.warning(f"Redis checkpoint save error: {e}")

    def list_checkpoints(self) -> list[dict]:
        return [
            self._checkpoints[cid].to_dict(include_state=False)
            for cid in reversed(self._ordered)
        ]

    def stats(self) -> dict:
        total_bytes = sum(c.size_bytes for c in self._checkpoints.values())
        return {
            **self._stats,
            "total_checkpoints": len(self._checkpoints),
            "total_bytes": total_bytes,
            "last_full_id": self._last_full_id or "",
        }


def build_jarvis_checkpoint_manager() -> CheckpointManager:
    return CheckpointManager(
        max_checkpoints=100,
        auto_interval_s=300.0,
        incremental_after=5,
    )


async def main():
    import sys

    mgr = build_jarvis_checkpoint_manager()
    await mgr.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Saving checkpoints...")

        state = {"counter": 0, "model": "qwen3.5-9b", "nodes": ["m1", "m2"]}
        c1 = mgr.save(state, name="initial", tags=["boot"])
        print(f"  Full checkpoint: {c1.ckpt_id} ({c1.size_bytes}B)")

        state["counter"] = 10
        state["status"] = "running"
        c2 = mgr.save(state, name="after-warmup")
        print(f"  {c2.kind.value} checkpoint: {c2.ckpt_id} ({c2.size_bytes}B)")

        state["counter"] = 25
        state["model"] = "deepseek-r1"
        c3 = mgr.save(state, name="model-switch")
        print(f"  {c3.kind.value} checkpoint: {c3.ckpt_id} ({c3.size_bytes}B)")

        # Rollback to initial
        restored = mgr.rollback_to(c1.ckpt_id)
        print(f"\nRolled back to {c1.ckpt_id}: {restored}")

        # Load incremental
        loaded = mgr.load(c3.ckpt_id)
        print(f"Loaded {c3.ckpt_id} (incremental reconstructed): {loaded}")

        print("\nCheckpoint list:")
        for ck in mgr.list_checkpoints():
            print(
                f"  [{ck['kind']:<5}] {ck['ckpt_id']} {ck['name']:<20} "
                f"{ck['size_bytes']}B tags={ck['tags']}"
            )

        print(f"\nStats: {json.dumps(mgr.stats(), indent=2)}")

    elif cmd == "list":
        for ck in mgr.list_checkpoints():
            print(json.dumps(ck))

    elif cmd == "stats":
        print(json.dumps(mgr.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
