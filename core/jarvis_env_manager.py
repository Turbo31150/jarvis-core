#!/usr/bin/env python3
"""
jarvis_env_manager — Environment and secrets management with masking
Manages env vars, .env files, secret rotation, and masked logging
"""

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.env_manager")

REDIS_PREFIX = "jarvis:env:"
ENV_FILE = Path("/home/turbo/IA/Core/jarvis/.env")

# Patterns that indicate sensitive values
SENSITIVE_PATTERNS = [
    r"(?i)(secret|password|passwd|token|key|api_key|auth|credential|private)",
    r"(?i)(bearer|oauth|jwt|x-api-key)",
]

MASK = "***REDACTED***"


@dataclass
class EnvVar:
    name: str
    value: str
    source: str  # file | env | runtime | redis
    sensitive: bool = False
    description: str = ""
    created_at: float = field(default_factory=time.time)
    rotated_at: float = 0.0

    @property
    def masked_value(self) -> str:
        return MASK if self.sensitive else self.value

    def to_dict(self, include_value: bool = False) -> dict:
        return {
            "name": self.name,
            "value": self.value if include_value else self.masked_value,
            "source": self.source,
            "sensitive": self.sensitive,
            "description": self.description,
        }


class EnvManager:
    def __init__(self, auto_mask: bool = True):
        self.redis: aioredis.Redis | None = None
        self._vars: dict[str, EnvVar] = {}
        self._sensitive_re = [re.compile(p) for p in SENSITIVE_PATTERNS]
        self.auto_mask = auto_mask
        self._stats: dict[str, int] = {
            "loaded": 0,
            "set": 0,
            "rotations": 0,
            "accesses": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def _is_sensitive(self, name: str) -> bool:
        return any(p.search(name) for p in self._sensitive_re)

    def load_env(self, prefix: str | None = None):
        """Load from os.environ."""
        for k, v in os.environ.items():
            if prefix and not k.startswith(prefix):
                continue
            self._vars[k] = EnvVar(
                name=k,
                value=v,
                source="env",
                sensitive=self.auto_mask and self._is_sensitive(k),
            )
            self._stats["loaded"] += 1

    def load_file(self, path: str | Path | None = None) -> int:
        """Parse .env file. Returns number of vars loaded."""
        p = Path(path or ENV_FILE)
        if not p.exists():
            return 0
        count = 0
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                name, _, value = line.partition("=")
                name = name.strip()
                value = value.strip().strip('"').strip("'")
                self._vars[name] = EnvVar(
                    name=name,
                    value=value,
                    source="file",
                    sensitive=self.auto_mask and self._is_sensitive(name),
                )
                count += 1
        self._stats["loaded"] += count
        log.debug(f"Loaded {count} vars from {p}")
        return count

    async def load_redis(self, prefix: str = ""):
        """Load secrets from Redis hash."""
        if not self.redis:
            return
        raw = await self.redis.hgetall(
            f"{REDIS_PREFIX}vars:{prefix}" if prefix else f"{REDIS_PREFIX}vars"
        )
        for k, v in raw.items():
            self._vars[k] = EnvVar(
                name=k,
                value=v,
                source="redis",
                sensitive=self.auto_mask and self._is_sensitive(k),
            )
            self._stats["loaded"] += 1

    def set(
        self,
        name: str,
        value: str,
        sensitive: bool | None = None,
        description: str = "",
        persist_redis: bool = False,
    ) -> EnvVar:
        is_sensitive = (
            sensitive
            if sensitive is not None
            else (self.auto_mask and self._is_sensitive(name))
        )
        var = EnvVar(
            name=name,
            value=value,
            source="runtime",
            sensitive=is_sensitive,
            description=description,
        )
        self._vars[name] = var
        self._stats["set"] += 1

        if persist_redis and self.redis:
            asyncio.create_task(self.redis.hset(f"{REDIS_PREFIX}vars", name, value))
        return var

    def get(self, name: str, default: str = "") -> str:
        self._stats["accesses"] += 1
        var = self._vars.get(name)
        if var:
            return var.value
        # Fallback to os.environ
        return os.environ.get(name, default)

    def get_int(self, name: str, default: int = 0) -> int:
        v = self.get(name, str(default))
        try:
            return int(v)
        except ValueError:
            return default

    def get_bool(self, name: str, default: bool = False) -> bool:
        v = self.get(name, "").lower()
        if v in ("true", "yes", "1"):
            return True
        if v in ("false", "no", "0"):
            return False
        return default

    def require(self, *names: str) -> dict[str, str]:
        """Get multiple vars, raising if any are missing."""
        result = {}
        missing = []
        for name in names:
            v = self.get(name)
            if not v:
                missing.append(name)
            else:
                result[name] = v
        if missing:
            raise KeyError(f"Required environment variables not set: {missing}")
        return result

    def rotate(self, name: str, new_value: str) -> EnvVar | None:
        var = self._vars.get(name)
        if not var:
            return None
        var.value = new_value
        var.rotated_at = time.time()
        self._stats["rotations"] += 1
        log.info(f"Secret rotated: {name}")
        if self.redis:
            asyncio.create_task(self.redis.hset(f"{REDIS_PREFIX}vars", name, new_value))
        return var

    def mask(self, text: str) -> str:
        """Replace all sensitive values in text with MASK."""
        for var in self._vars.values():
            if var.sensitive and var.value and len(var.value) > 3:
                text = text.replace(var.value, MASK)
        return text

    def export_env(self, sensitive: bool = False) -> dict[str, str]:
        return {
            k: (v.value if sensitive else v.masked_value) for k, v in self._vars.items()
        }

    def list_vars(self, include_sensitive: bool = False) -> list[dict]:
        return [v.to_dict(include_value=include_sensitive) for v in self._vars.values()]

    def validate_required(self, required: list[str]) -> tuple[bool, list[str]]:
        missing = [name for name in required if not self.get(name)]
        return len(missing) == 0, missing

    def stats(self) -> dict:
        sensitive_count = sum(1 for v in self._vars.values() if v.sensitive)
        return {
            **self._stats,
            "total_vars": len(self._vars),
            "sensitive_vars": sensitive_count,
            "sources": {
                s: sum(1 for v in self._vars.values() if v.source == s)
                for s in ("file", "env", "runtime", "redis")
            },
        }


# Global instance
_env = EnvManager()


def get_env() -> EnvManager:
    return _env


async def main():
    import sys

    mgr = EnvManager()
    await mgr.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        # Set some vars
        mgr.set("JARVIS_HOST", "192.168.1.85")
        mgr.set("JARVIS_PORT", "8080")
        mgr.set("JARVIS_API_KEY", "sk-test-1234567890abcdef", sensitive=True)
        mgr.set("REDIS_PASSWORD", "super-secret", sensitive=True)
        mgr.set("DEBUG", "true")

        # Load from os.environ
        mgr.load_env(prefix="PATH")

        print("Vars (masked):")
        for v in mgr.list_vars():
            print(
                f"  {v['name']:<25} = {v['value']} [{v['source']}]{'🔒' if v['sensitive'] else ''}"
            )

        # Test masking
        log_line = (
            f"Connecting with key sk-test-1234567890abcdef to {mgr.get('JARVIS_HOST')}"
        )
        print(f"\nOriginal: {log_line}")
        print(f"Masked:   {mgr.mask(log_line)}")

        # Test require
        try:
            vals = mgr.require("JARVIS_HOST", "JARVIS_PORT")
            print(f"\nRequired vars: {vals}")
        except KeyError as e:
            print(f"Missing: {e}")

        # Rotation
        mgr.rotate("JARVIS_API_KEY", "sk-new-key-rotated")
        print(f"\nRotations: {mgr._stats['rotations']}")
        print(f"Stats: {json.dumps(mgr.stats(), indent=2)}")

    elif cmd == "list":
        mgr.load_env()
        mgr.load_file()
        for v in mgr.list_vars():
            print(f"  {v['name']:<30} = {v['value']}")

    elif cmd == "stats":
        print(json.dumps(mgr.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
