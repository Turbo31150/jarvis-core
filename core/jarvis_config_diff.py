#!/usr/bin/env python3
"""
jarvis_config_diff — Configuration change diffing and audit for JARVIS cluster
Detects config drift, shows structured diffs, tracks who changed what
"""

import asyncio
import copy
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.config_diff")

REDIS_PREFIX = "jarvis:cfgdiff:"
SNAPSHOTS_FILE = Path("/home/turbo/IA/Core/jarvis/data/config_snapshots.jsonl")


class ChangeType(str, Enum):
    ADDED = "added"
    REMOVED = "removed"
    MODIFIED = "modified"
    UNCHANGED = "unchanged"


@dataclass
class FieldChange:
    path: str  # dot-notation path: "cluster.m1.url"
    change_type: ChangeType
    old_value: object = None
    new_value: object = None

    def to_dict(self) -> dict:
        d: dict = {"path": self.path, "type": self.change_type.value}
        if self.change_type == ChangeType.MODIFIED:
            d["old"] = self.old_value
            d["new"] = self.new_value
        elif self.change_type == ChangeType.ADDED:
            d["value"] = self.new_value
        elif self.change_type == ChangeType.REMOVED:
            d["value"] = self.old_value
        return d


@dataclass
class ConfigSnapshot:
    snapshot_id: str
    config: dict
    author: str = "system"
    description: str = ""
    content_hash: str = ""
    ts: float = field(default_factory=time.time)

    def __post_init__(self):
        if not self.content_hash:
            self.content_hash = _hash_config(self.config)

    def to_dict(self, include_config: bool = False) -> dict:
        d = {
            "snapshot_id": self.snapshot_id,
            "author": self.author,
            "description": self.description,
            "content_hash": self.content_hash,
            "ts": self.ts,
        }
        if include_config:
            d["config"] = self.config
        return d


@dataclass
class DiffReport:
    from_snapshot: str
    to_snapshot: str
    changes: list[FieldChange] = field(default_factory=list)
    ts: float = field(default_factory=time.time)

    @property
    def has_changes(self) -> bool:
        return len(self.changes) > 0

    @property
    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for c in self.changes:
            counts[c.change_type.value] = counts.get(c.change_type.value, 0) + 1
        return counts

    def to_dict(self) -> dict:
        return {
            "from": self.from_snapshot,
            "to": self.to_snapshot,
            "has_changes": self.has_changes,
            "summary": self.summary,
            "changes": [c.to_dict() for c in self.changes],
            "ts": self.ts,
        }


def _hash_config(config: dict) -> str:
    serialized = json.dumps(config, sort_keys=True)
    return hashlib.md5(serialized.encode()).hexdigest()[:16]


def _deep_diff(old: object, new: object, path: str = "") -> list[FieldChange]:
    """Recursively diff two config dicts."""
    changes: list[FieldChange] = []

    if isinstance(old, dict) and isinstance(new, dict):
        all_keys = set(old) | set(new)
        for key in sorted(all_keys):
            child_path = f"{path}.{key}" if path else key
            if key not in old:
                changes.append(
                    FieldChange(child_path, ChangeType.ADDED, new_value=new[key])
                )
            elif key not in new:
                changes.append(
                    FieldChange(child_path, ChangeType.REMOVED, old_value=old[key])
                )
            else:
                changes.extend(_deep_diff(old[key], new[key], child_path))
    elif isinstance(old, list) and isinstance(new, list):
        if old != new:
            changes.append(
                FieldChange(path, ChangeType.MODIFIED, old_value=old, new_value=new)
            )
    else:
        if old != new:
            changes.append(
                FieldChange(path, ChangeType.MODIFIED, old_value=old, new_value=new)
            )

    return changes


class ConfigDiffTracker:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._snapshots: list[ConfigSnapshot] = []
        self._stats: dict[str, int] = {
            "snapshots": 0,
            "diffs_computed": 0,
            "total_changes": 0,
        }
        SNAPSHOTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def _load(self):
        if not SNAPSHOTS_FILE.exists():
            return
        try:
            for line in SNAPSHOTS_FILE.read_text().splitlines():
                if not line.strip():
                    continue
                d = json.loads(line)
                snap = ConfigSnapshot(
                    snapshot_id=d["snapshot_id"],
                    config=d.get("config", {}),
                    author=d.get("author", "system"),
                    description=d.get("description", ""),
                    content_hash=d.get("content_hash", ""),
                    ts=d.get("ts", time.time()),
                )
                self._snapshots.append(snap)
        except Exception as e:
            log.warning(f"Config snapshot load error: {e}")

    def snapshot(
        self,
        config: dict,
        author: str = "system",
        description: str = "",
        snapshot_id: str | None = None,
    ) -> ConfigSnapshot:
        import uuid

        sid = snapshot_id or str(uuid.uuid4())[:12]
        snap = ConfigSnapshot(
            snapshot_id=sid,
            config=copy.deepcopy(config),
            author=author,
            description=description,
        )

        # Skip if identical to last snapshot
        if self._snapshots and self._snapshots[-1].content_hash == snap.content_hash:
            log.debug("Config unchanged — skipping snapshot")
            return self._snapshots[-1]

        self._snapshots.append(snap)
        self._stats["snapshots"] += 1

        try:
            with open(SNAPSHOTS_FILE, "a") as f:
                f.write(json.dumps(snap.to_dict(include_config=True)) + "\n")
        except Exception as e:
            log.warning(f"Snapshot save error: {e}")

        if self.redis:
            asyncio.create_task(
                self.redis.setex(
                    f"{REDIS_PREFIX}latest",
                    3600,
                    json.dumps(snap.to_dict()),
                )
            )
        return snap

    def diff(self, snap_id_a: str, snap_id_b: str) -> DiffReport:
        a = self._get(snap_id_a)
        b = self._get(snap_id_b)
        if not a or not b:
            return DiffReport(snap_id_a, snap_id_b)

        self._stats["diffs_computed"] += 1
        changes = _deep_diff(a.config, b.config)
        self._stats["total_changes"] += len(changes)
        return DiffReport(
            from_snapshot=snap_id_a, to_snapshot=snap_id_b, changes=changes
        )

    def diff_latest(self) -> DiffReport | None:
        if len(self._snapshots) < 2:
            return None
        a = self._snapshots[-2]
        b = self._snapshots[-1]
        return self.diff(a.snapshot_id, b.snapshot_id)

    def diff_from_baseline(self, baseline_id: str) -> DiffReport | None:
        if not self._snapshots:
            return None
        return self.diff(baseline_id, self._snapshots[-1].snapshot_id)

    def detect_drift(self, expected: dict) -> list[FieldChange]:
        """Compare expected config against latest snapshot."""
        if not self._snapshots:
            return []
        return _deep_diff(self._snapshots[-1].config, expected)

    def _get(self, snapshot_id: str) -> ConfigSnapshot | None:
        return next((s for s in self._snapshots if s.snapshot_id == snapshot_id), None)

    def latest(self) -> ConfigSnapshot | None:
        return self._snapshots[-1] if self._snapshots else None

    def list_snapshots(self, n: int = 20) -> list[dict]:
        return [s.to_dict() for s in self._snapshots[-n:]]

    def stats(self) -> dict:
        return {**self._stats}


def build_jarvis_config_diff() -> ConfigDiffTracker:
    return ConfigDiffTracker()


async def main():
    import sys

    tracker = build_jarvis_config_diff()
    await tracker.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        # Initial config
        cfg_v1 = {
            "cluster": {
                "m1": {
                    "url": "http://192.168.1.85:1234",
                    "weight": 1.2,
                    "enabled": True,
                },
                "m2": {
                    "url": "http://192.168.1.26:1234",
                    "weight": 1.0,
                    "enabled": True,
                },
                "ol1": {
                    "url": "http://127.0.0.1:11434",
                    "weight": 0.5,
                    "enabled": True,
                },
            },
            "features": {"rag_enabled": False, "streaming": True},
            "limits": {"max_tokens": 4096, "timeout_s": 30},
        }
        s1 = tracker.snapshot(cfg_v1, author="system", description="Initial config")
        print(f"Snapshot 1: {s1.snapshot_id} hash={s1.content_hash}")

        # Modified config
        cfg_v2 = {
            "cluster": {
                "m1": {
                    "url": "http://192.168.1.85:1234",
                    "weight": 1.5,
                    "enabled": True,
                },
                "m2": {
                    "url": "http://192.168.1.26:1234",
                    "weight": 1.0,
                    "enabled": False,
                },
                "m3": {
                    "url": "http://192.168.1.133:1234",
                    "weight": 0.8,
                    "enabled": True,
                },
            },
            "features": {"rag_enabled": True, "streaming": True},
            "limits": {"max_tokens": 8192, "timeout_s": 30},
        }
        s2 = tracker.snapshot(
            cfg_v2, author="turbo", description="Enable RAG + M3, disable M2"
        )
        print(f"Snapshot 2: {s2.snapshot_id} hash={s2.content_hash}")

        report = tracker.diff(s1.snapshot_id, s2.snapshot_id)
        print(f"\nDiff {s1.snapshot_id} → {s2.snapshot_id}:")
        print(f"  Summary: {report.summary}")
        for change in report.changes:
            icon = {"added": "➕", "removed": "➖", "modified": "✏️"}.get(
                change.change_type.value, "·"
            )
            d = change.to_dict()
            if change.change_type == ChangeType.MODIFIED:
                print(f"  {icon} {change.path}: {d['old']} → {d['new']}")
            else:
                print(f"  {icon} {change.path}: {d.get('value')}")

        print(f"\nStats: {json.dumps(tracker.stats(), indent=2)}")

    elif cmd == "list":
        for s in tracker.list_snapshots():
            print(f"  {s['snapshot_id']} {s['author']:<12} {s['description'][:40]}")

    elif cmd == "stats":
        print(json.dumps(tracker.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
