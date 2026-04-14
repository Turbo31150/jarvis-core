#!/usr/bin/env python3
"""
jarvis_config_store — Centralized dynamic config with hot-reload
Layered config: defaults < file < Redis < runtime overrides. Watch for changes.
"""

import asyncio
import json
import logging
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.config_store")

CONFIG_FILE = Path("/home/turbo/IA/Core/jarvis/data/jarvis_config.json")
REDIS_KEY = "jarvis:config"
REDIS_WATCH_CHANNEL = "jarvis:config:updates"

DEFAULTS: dict[str, Any] = {
    # Inference
    "inference.default_model": "qwen/qwen3.5-9b",
    "inference.default_backend": "M1",
    "inference.timeout_s": 120,
    "inference.max_tokens": 2048,
    "inference.temperature": 0.0,
    # GPU
    "gpu.thermal_target_3080": 75,
    "gpu.thermal_target_2060": 72,
    "gpu.thermal_target_1660s": 70,
    "gpu.pl_3080": 200,
    "gpu.pl_2060": 125,
    "gpu.pl_1660s": 65,
    # Cache
    "cache.query_ttl_s": 3600,
    "cache.session_ttl_s": 86400,
    "cache.max_entries": 10000,
    # Budget
    "budget.daily_claude_tokens": 50000,
    "budget.daily_openai_tokens": 100000,
    "budget.alert_threshold_pct": 90,
    # Pipeline
    "pipeline.stall_threshold_s": 60,
    "pipeline.timeout_s": 300,
    "pipeline.max_concurrent": 10,
    # Cluster
    "cluster.m1_url": "http://127.0.0.1:1234",
    "cluster.m2_url": "http://192.168.1.26:1234",
    "cluster.ol1_url": "http://127.0.0.1:11434",
    "cluster.health_interval_s": 30,
    # Logging
    "log.level": "INFO",
    "log.max_lines": 10000,
}


class ConfigStore:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._config: dict[str, Any] = deepcopy(DEFAULTS)
        self._overrides: dict[str, Any] = {}
        self._watchers: list[asyncio.Queue] = []
        self._loaded_at: float = 0.0
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    async def load(self):
        """Load config from file then Redis (Redis takes precedence)."""
        # Load file
        if CONFIG_FILE.exists():
            try:
                file_cfg = json.loads(CONFIG_FILE.read_text())
                self._config.update(file_cfg)
                log.debug(f"Loaded {len(file_cfg)} keys from file")
            except Exception as e:
                log.warning(f"Config file load error: {e}")

        # Load Redis
        if self.redis:
            try:
                raw = await self.redis.get(REDIS_KEY)
                if raw:
                    redis_cfg = json.loads(raw)
                    self._config.update(redis_cfg)
                    log.debug(f"Loaded {len(redis_cfg)} keys from Redis")
            except Exception as e:
                log.warning(f"Config Redis load error: {e}")

        # Apply overrides
        self._config.update(self._overrides)
        self._loaded_at = time.time()

    async def save(self):
        """Persist current config to file and Redis."""
        # Save to file (exclude defaults that match)
        non_default = {k: v for k, v in self._config.items() if DEFAULTS.get(k) != v}
        CONFIG_FILE.write_text(json.dumps(non_default, indent=2))

        # Save to Redis
        if self.redis:
            await self.redis.set(REDIS_KEY, json.dumps(self._config))
            await self.redis.publish(
                REDIS_WATCH_CHANNEL,
                json.dumps(
                    {
                        "event": "config_updated",
                        "keys": list(non_default.keys()),
                        "ts": time.time(),
                    }
                ),
            )

    def get(self, key: str, default: Any = None) -> Any:
        """Get config value. Supports dot-notation nested keys."""
        if key in self._overrides:
            return self._overrides[key]
        return self._config.get(key, default)

    def get_int(self, key: str, default: int = 0) -> int:
        return int(self.get(key, default))

    def get_float(self, key: str, default: float = 0.0) -> float:
        return float(self.get(key, default))

    def get_bool(self, key: str, default: bool = False) -> bool:
        v = self.get(key, default)
        if isinstance(v, bool):
            return v
        return str(v).lower() in ("true", "1", "yes")

    def get_section(self, prefix: str) -> dict[str, Any]:
        """Get all keys starting with prefix."""
        return {
            k[len(prefix) + 1 :]: v
            for k, v in self._config.items()
            if k.startswith(f"{prefix}.")
        }

    async def set(self, key: str, value: Any, persist: bool = True):
        self._config[key] = value
        if persist:
            await self.save()
        # Notify watchers
        for q in self._watchers:
            await q.put({"key": key, "value": value})

    def set_override(self, key: str, value: Any):
        """Runtime override — not persisted, highest priority."""
        self._overrides[key] = value
        self._config[key] = value

    def reset(self, key: str) -> bool:
        if key in self._overrides:
            del self._overrides[key]
        if key in self._config:
            self._config[key] = DEFAULTS.get(key)
            return True
        return False

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._watchers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self._watchers = [w for w in self._watchers if w is not q]

    async def watch_redis(self):
        """Watch Redis channel for config updates from other nodes."""
        if not self.redis:
            return
        async with self.redis.pubsub() as ps:
            await ps.subscribe(REDIS_WATCH_CHANNEL)
            async for msg in ps.listen():
                if msg["type"] != "message":
                    continue
                try:
                    d = json.loads(msg["data"])
                    if d.get("event") == "config_updated":
                        await self.load()
                        log.info(f"Config hot-reloaded: {d.get('keys')}")
                except Exception:
                    pass

    def dump(self) -> dict:
        return {
            "loaded_at": self._loaded_at,
            "keys_total": len(self._config),
            "overrides": len(self._overrides),
            "config": self._config,
        }


# ── Global singleton ──────────────────────────────────────────────────────────

_store: ConfigStore | None = None


async def get_store() -> ConfigStore:
    global _store
    if _store is None:
        _store = ConfigStore()
        await _store.connect_redis()
        await _store.load()
    return _store


# ── CLI ───────────────────────────────────────────────────────────────────────


async def main():
    import sys

    store = ConfigStore()
    await store.connect_redis()
    await store.load()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"

    if cmd == "list":
        cfg = store._config
        print(f"{'Key':<45} {'Value'}")
        print("-" * 75)
        for k in sorted(cfg):
            marker = "  " if cfg[k] == DEFAULTS.get(k) else "* "
            print(f"{marker}{k:<43} {cfg[k]}")
        print(f"\n* = non-default | {len(cfg)} total keys")

    elif cmd == "get" and len(sys.argv) > 2:
        key = sys.argv[2]
        val = store.get(key)
        print(f"{key} = {val!r}")

    elif cmd == "set" and len(sys.argv) > 3:
        key = sys.argv[2]
        raw_val = sys.argv[3]
        # Auto-type
        try:
            val: Any = json.loads(raw_val)
        except Exception:
            val = raw_val
        await store.set(key, val)
        print(f"Set {key} = {val!r}")

    elif cmd == "reset" and len(sys.argv) > 2:
        ok = store.reset(sys.argv[2])
        if ok:
            await store.save()
            print(f"Reset {sys.argv[2]} → {store.get(sys.argv[2])!r}")
        else:
            print(f"Key not found: {sys.argv[2]}")

    elif cmd == "section" and len(sys.argv) > 2:
        section = store.get_section(sys.argv[2])
        print(json.dumps(section, indent=2))

    elif cmd == "dump":
        d = store.dump()
        d.pop("config")  # too verbose
        print(json.dumps(d, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
