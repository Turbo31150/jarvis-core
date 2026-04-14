#!/usr/bin/env python3
"""
jarvis_config_versioner — Configuration versioning with diff, rollback, and audit
Tracks all config changes with who changed what, when, and why
"""

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.config_versioner")

VERSIONS_FILE = Path("/home/turbo/IA/Core/jarvis/data/config_versions.jsonl")
REDIS_PREFIX = "jarvis:cfgver:"


class ChangeKind(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    ROLLBACK = "rollback"
    IMPORT = "import"


@dataclass
class ConfigVersion:
    version_id: str
    config_key: str
    version: int
    data: Any
    checksum: str
    author: str
    message: str
    kind: ChangeKind
    ts: float = field(default_factory=time.time)
    parent_version_id: str = ""
    tags: list[str] = field(default_factory=list)

    def to_dict(self, include_data: bool = True) -> dict:
        d = {
            "version_id": self.version_id,
            "config_key": self.config_key,
            "version": self.version,
            "checksum": self.checksum,
            "author": self.author,
            "message": self.message,
            "kind": self.kind.value,
            "ts": self.ts,
            "parent_version_id": self.parent_version_id,
            "tags": self.tags,
        }
        if include_data:
            d["data"] = self.data
        return d


def _checksum(data: Any) -> str:
    return hashlib.md5(json.dumps(data, sort_keys=True).encode()).hexdigest()[:12]


def _diff(old: Any, new: Any, path: str = "") -> list[dict]:
    """Produce a list of diff entries between two JSON-serializable values."""
    diffs = []
    if isinstance(old, dict) and isinstance(new, dict):
        all_keys = set(old) | set(new)
        for k in sorted(all_keys):
            sub = f"{path}.{k}" if path else k
            if k not in old:
                diffs.append({"op": "add", "path": sub, "value": new[k]})
            elif k not in new:
                diffs.append({"op": "remove", "path": sub, "old": old[k]})
            else:
                diffs.extend(_diff(old[k], new[k], sub))
    elif old != new:
        diffs.append({"op": "replace", "path": path or "/", "old": old, "new": new})
    return diffs


class ConfigVersioner:
    def __init__(self, persist: bool = True):
        self.redis: aioredis.Redis | None = None
        # {config_key: [ConfigVersion sorted by version]}
        self._store: dict[str, list[ConfigVersion]] = {}
        self._persist = persist
        self._stats: dict[str, int] = {
            "writes": 0,
            "rollbacks": 0,
            "reads": 0,
        }
        if persist:
            VERSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def _next_version(self, config_key: str) -> int:
        history = self._store.get(config_key, [])
        return (history[-1].version + 1) if history else 1

    def _make_id(self, config_key: str, version: int) -> str:
        return f"{config_key}:{version}"

    def put(
        self,
        config_key: str,
        data: Any,
        author: str = "system",
        message: str = "",
        tags: list[str] | None = None,
        kind: ChangeKind = ChangeKind.UPDATE,
    ) -> ConfigVersion:
        existing = self._store.get(config_key, [])
        new_checksum = _checksum(data)

        # Skip if data unchanged
        if existing and existing[-1].checksum == new_checksum:
            log.debug(f"Config {config_key!r} unchanged, skipping write")
            return existing[-1]

        version_num = self._next_version(config_key)
        if not existing:
            kind = ChangeKind.CREATE

        parent_id = existing[-1].version_id if existing else ""

        ver = ConfigVersion(
            version_id=self._make_id(config_key, version_num),
            config_key=config_key,
            version=version_num,
            data=data,
            checksum=new_checksum,
            author=author,
            message=message or f"{kind.value} by {author}",
            kind=kind,
            parent_version_id=parent_id,
            tags=tags or [],
        )

        self._store.setdefault(config_key, []).append(ver)
        self._stats["writes"] += 1

        if self._persist:
            try:
                with open(VERSIONS_FILE, "a") as f:
                    f.write(json.dumps(ver.to_dict()) + "\n")
            except Exception:
                pass

        if self.redis:
            asyncio.create_task(self._redis_put(ver))

        log.debug(f"Config {config_key!r} v{version_num} by {author}: {message[:50]}")
        return ver

    async def _redis_put(self, ver: ConfigVersion):
        if not self.redis:
            return
        try:
            key = f"{REDIS_PREFIX}{ver.config_key}"
            await self.redis.setex(key, 3600, json.dumps(ver.to_dict()))
            await self.redis.setex(
                f"{REDIS_PREFIX}{ver.config_key}:v{ver.version}",
                86400,
                json.dumps(ver.to_dict()),
            )
        except Exception:
            pass

    def get(self, config_key: str, version: int | None = None) -> Any:
        self._stats["reads"] += 1
        history = self._store.get(config_key, [])
        if not history:
            return None
        if version is None:
            return history[-1].data
        for ver in history:
            if ver.version == version:
                return ver.data
        return None

    def current_version(self, config_key: str) -> ConfigVersion | None:
        history = self._store.get(config_key, [])
        return history[-1] if history else None

    def history(self, config_key: str, limit: int = 20) -> list[dict]:
        history = self._store.get(config_key, [])
        return [v.to_dict(include_data=False) for v in history[-limit:]]

    def diff(self, config_key: str, version_a: int, version_b: int) -> list[dict]:
        a = self.get(config_key, version_a)
        b = self.get(config_key, version_b)
        if a is None or b is None:
            return []
        return _diff(a, b)

    def rollback(
        self,
        config_key: str,
        target_version: int,
        author: str = "system",
        message: str = "",
    ) -> ConfigVersion | None:
        data = self.get(config_key, target_version)
        if data is None:
            log.warning(f"Rollback failed: {config_key} v{target_version} not found")
            return None

        self._stats["rollbacks"] += 1
        return self.put(
            config_key,
            data,
            author=author,
            message=message or f"Rollback to v{target_version}",
            kind=ChangeKind.ROLLBACK,
        )

    def delete(self, config_key: str, author: str = "system"):
        if config_key not in self._store:
            return
        last = self._store[config_key][-1]
        self.put(
            config_key, None, author=author, message="deleted", kind=ChangeKind.DELETE
        )

    def keys(self) -> list[str]:
        return list(self._store.keys())

    def snapshot(self) -> dict[str, Any]:
        return {k: self.get(k) for k in self._store if self.get(k) is not None}

    def stats(self) -> dict:
        return {
            **self._stats,
            "config_keys": len(self._store),
            "total_versions": sum(len(v) for v in self._store.values()),
        }


def build_jarvis_config_versioner() -> ConfigVersioner:
    versioner = ConfigVersioner(persist=True)

    # Seed with initial cluster config
    versioner.put(
        "cluster.nodes",
        {
            "m1": {"ip": "192.168.1.85", "port": 1234, "tier": "fast"},
            "m2": {"ip": "192.168.1.26", "port": 1234, "tier": "large"},
            "ol1": {"ip": "127.0.0.1", "port": 11434, "tier": "local"},
        },
        author="bootstrap",
        message="Initial cluster node config",
        tags=["cluster", "infra"],
    )
    versioner.put(
        "inference.defaults",
        {"temperature": 0.7, "max_tokens": 2048, "timeout_s": 30.0},
        author="bootstrap",
        message="Default inference parameters",
        tags=["inference"],
    )
    versioner.put(
        "budget.global",
        {"max_tokens_day": 10_000_000, "max_cost_usd_day": 5.0},
        author="bootstrap",
        message="Global daily budget",
        tags=["budget"],
    )

    return versioner


async def main():
    import sys

    versioner = build_jarvis_config_versioner()
    await versioner.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Config versioner demo...")

        # Update a config
        versioner.put(
            "inference.defaults",
            {"temperature": 0.5, "max_tokens": 4096, "timeout_s": 45.0},
            author="turbo",
            message="Increase max_tokens for longer responses",
        )

        # Show history
        print("\nHistory for 'inference.defaults':")
        for entry in versioner.history("inference.defaults"):
            print(f"  v{entry['version']} by {entry['author']}: {entry['message']}")

        # Show diff
        print("\nDiff v1→v2:")
        for d in versioner.diff("inference.defaults", 1, 2):
            print(
                f"  {d['op']:8} {d['path']}: {d.get('old', '—')} → {d.get('new', d.get('value', ''))}"
            )

        # Rollback
        versioner.rollback(
            "inference.defaults",
            1,
            author="turbo",
            message="Revert to conservative settings",
        )
        print(f"\nAfter rollback: {versioner.get('inference.defaults')}")

        print(f"\nAll config keys: {versioner.keys()}")
        print(f"Stats: {json.dumps(versioner.stats(), indent=2)}")

    elif cmd == "keys":
        print(json.dumps(versioner.keys()))

    elif cmd == "stats":
        print(json.dumps(versioner.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
