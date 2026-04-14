#!/usr/bin/env python3
"""
jarvis_config_loader — Layered configuration with hot-reload and validation
Merges defaults < file < env vars < runtime overrides with type coercion
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.config_loader")

REDIS_PREFIX = "jarvis:config:"
CONFIG_FILE = Path("/home/turbo/IA/Core/jarvis/data/jarvis_config.json")


@dataclass
class ConfigChange:
    key: str
    old_value: Any
    new_value: Any
    source: str
    ts: float = field(default_factory=time.time)


class ConfigLoader:
    def __init__(self, namespace: str = "jarvis"):
        self.redis: aioredis.Redis | None = None
        self.namespace = namespace
        self._layers: dict[str, dict] = {
            "defaults": {},
            "file": {},
            "env": {},
            "redis": {},
            "runtime": {},
        }
        self._resolved: dict[str, Any] = {}
        self._watchers: list[Any] = []  # callbacks for changes
        self._history: list[ConfigChange] = []
        self._file_mtime: float = 0.0
        self._stats: dict[str, int] = {"loads": 0, "reloads": 0, "changes": 0}

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
            # Load Redis layer
            raw = await self.redis.hgetall(f"{REDIS_PREFIX}{self.namespace}")
            for k, v in raw.items():
                try:
                    self._layers["redis"][k] = json.loads(v)
                except Exception:
                    self._layers["redis"][k] = v
        except Exception:
            self.redis = None

    def set_defaults(self, defaults: dict):
        self._layers["defaults"] = self._flatten(defaults)
        self._resolve()

    def load_file(self, path: str | Path | None = None) -> bool:
        p = Path(path or CONFIG_FILE)
        if not p.exists():
            return False
        try:
            data = json.loads(p.read_text())
            self._layers["file"] = self._flatten(data)
            self._file_mtime = p.stat().st_mtime
            self._resolve()
            self._stats["loads"] += 1
            log.debug(f"Config loaded from {p}: {len(self._layers['file'])} keys")
            return True
        except Exception as e:
            log.warning(f"Config file load error: {e}")
            return False

    def load_env(self, prefix: str = "JARVIS_"):
        env_layer: dict[str, Any] = {}
        for k, v in os.environ.items():
            if k.startswith(prefix):
                key = k[len(prefix) :].lower().replace("__", ".")
                env_layer[key] = self._coerce(v)
        self._layers["env"] = env_layer
        self._resolve()

    def _coerce(self, value: str) -> Any:
        """Try to coerce string to bool/int/float/json."""
        if value.lower() in ("true", "yes", "1"):
            return True
        if value.lower() in ("false", "no", "0"):
            return False
        try:
            return int(value)
        except ValueError:
            pass
        try:
            return float(value)
        except ValueError:
            pass
        try:
            return json.loads(value)
        except Exception:
            pass
        return value

    def _flatten(self, d: dict, prefix: str = "") -> dict:
        """Flatten nested dict to dot-notation keys."""
        result: dict[str, Any] = {}
        for k, v in d.items():
            full_key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                result.update(self._flatten(v, full_key))
            else:
                result[full_key] = v
        return result

    def _resolve(self):
        """Merge all layers, later layers override earlier ones."""
        old = dict(self._resolved)
        merged: dict[str, Any] = {}
        for layer_name in ["defaults", "file", "env", "redis", "runtime"]:
            merged.update(self._layers[layer_name])
        self._resolved = merged

        # Detect changes
        all_keys = set(old.keys()) | set(merged.keys())
        for key in all_keys:
            old_val = old.get(key)
            new_val = merged.get(key)
            if old_val != new_val:
                source = next(
                    (
                        ln
                        for ln in reversed(
                            ["defaults", "file", "env", "redis", "runtime"]
                        )
                        if key in self._layers[ln]
                    ),
                    "unknown",
                )
                change = ConfigChange(
                    key=key, old_value=old_val, new_value=new_val, source=source
                )
                self._history.append(change)
                self._stats["changes"] += 1
                for watcher in self._watchers:
                    try:
                        watcher(change)
                    except Exception:
                        pass

    def set(self, key: str, value: Any, persist: bool = False):
        self._layers["runtime"][key] = value
        self._resolve()
        if persist and self.redis:
            asyncio.create_task(
                self.redis.hset(
                    f"{REDIS_PREFIX}{self.namespace}", key, json.dumps(value)
                )
            )

    def get(self, key: str, default: Any = None) -> Any:
        return self._resolved.get(key, default)

    def get_int(self, key: str, default: int = 0) -> int:
        return int(self.get(key, default))

    def get_float(self, key: str, default: float = 0.0) -> float:
        return float(self.get(key, default))

    def get_bool(self, key: str, default: bool = False) -> bool:
        v = self.get(key, default)
        if isinstance(v, bool):
            return v
        return str(v).lower() in ("true", "yes", "1")

    def get_list(self, key: str, default: list | None = None) -> list:
        v = self.get(key, default or [])
        if isinstance(v, list):
            return v
        return [v]

    def on_change(self, callback):
        self._watchers.append(callback)

    def check_reload(self) -> bool:
        """Reload file if mtime changed. Returns True if reloaded."""
        p = Path(CONFIG_FILE)
        if p.exists():
            mtime = p.stat().st_mtime
            if mtime != self._file_mtime:
                self.load_file(p)
                self._stats["reloads"] += 1
                return True
        return False

    def dump(self) -> dict:
        return dict(self._resolved)

    def dump_by_layer(self) -> dict:
        return {ln: dict(layer) for ln, layer in self._layers.items()}

    def stats(self) -> dict:
        return {
            **self._stats,
            "keys": len(self._resolved),
            "layers": {ln: len(layer) for ln, layer in self._layers.items()},
            "history": len(self._history),
        }


# Global config instance
_config = ConfigLoader()


def get_config() -> ConfigLoader:
    return _config


async def main():
    import sys

    cfg = ConfigLoader()
    await cfg.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        cfg.set_defaults(
            {
                "server": {"host": "0.0.0.0", "port": 8080, "workers": 4},
                "llm": {
                    "default_model": "qwen3.5-9b",
                    "timeout_s": 30,
                    "max_tokens": 2048,
                },
                "redis": {"host": "127.0.0.1", "port": 6379},
                "debug": False,
            }
        )

        # Override via runtime
        cfg.set("llm.default_model", "qwen3.5-27b-claude")
        cfg.set("server.port", 9000)

        # Track changes
        changes = []
        cfg.on_change(
            lambda c: changes.append(f"{c.key}: {c.old_value} → {c.new_value}")
        )
        cfg.set("debug", True)

        print(f"server.port: {cfg.get_int('server.port')}")
        print(f"llm.default_model: {cfg.get('llm.default_model')}")
        print(f"debug: {cfg.get_bool('debug')}")
        print(f"Changes: {changes}")
        print(f"\nStats: {json.dumps(cfg.stats(), indent=2)}")

    elif cmd == "dump":
        cfg.load_file()
        cfg.load_env()
        print(json.dumps(cfg.dump(), indent=2))

    elif cmd == "stats":
        print(json.dumps(cfg.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
