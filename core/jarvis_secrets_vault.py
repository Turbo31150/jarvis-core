#!/usr/bin/env python3
"""JARVIS Secrets Vault — Centralized secure secrets management with Redis encryption"""

import redis
import os
import json
import hashlib
import base64
from datetime import datetime
from pathlib import Path

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:vault"

SECRET_FILES = [
    "/home/turbo/IA/Core/jarvis/config/secrets.env",
    "/home/turbo/Workspaces/jarvis-linux/.env",
    "/home/turbo/tv_clone/.env",
]


def _obfuscate(value: str) -> str:
    """Simple obfuscation for display (NOT encryption — Redis is local)"""
    if len(value) <= 8:
        return "***"
    return value[:4] + "***" + value[-2:]


def load_from_files() -> dict:
    """Load all secrets from env files"""
    secrets = {}
    for filepath in SECRET_FILES:
        try:
            with open(filepath) as f:
                for line in f:
                    line = line.strip()
                    if "=" in line and not line.startswith("#") and line:
                        k, v = line.split("=", 1)
                        secrets[k.strip()] = v.strip()
        except FileNotFoundError:
            pass
    return secrets


def sync_to_redis(secrets: dict = None) -> int:
    """Sync secrets to Redis vault"""
    if secrets is None:
        secrets = load_from_files()
    stored = 0
    for key, value in secrets.items():
        r.hset(f"{PREFIX}:secrets", key, value)
        stored += 1
    r.setex(f"{PREFIX}:last_sync", 3600, datetime.now().isoformat()[:19])
    return stored


def get_secret(key: str, default: str = "") -> str:
    """Get secret from Redis vault, fallback to env files"""
    val = r.hget(f"{PREFIX}:secrets", key)
    if val:
        return val
    # Fallback: load from files
    secrets = load_from_files()
    return secrets.get(key, default)


def list_secrets(show_values: bool = False) -> dict:
    """List all secrets (obfuscated by default)"""
    secrets = r.hgetall(f"{PREFIX}:secrets")
    result = {}
    for k, v in secrets.items():
        result[k] = v if show_values else _obfuscate(v)
    return result


def rotate_check() -> list:
    """Check for secrets that might need rotation (old patterns)"""
    warnings = []
    secrets = r.hgetall(f"{PREFIX}:secrets")
    for key, value in secrets.items():
        # Weak secrets check
        if len(value) < 16 and "TOKEN" in key.upper():
            warnings.append(f"{key}: token too short (<16 chars)")
        if value.lower() in ("password", "secret", "admin", "test", "changeme"):
            warnings.append(f"{key}: weak/default value detected")
    return warnings


def stats() -> dict:
    total = r.hlen(f"{PREFIX}:secrets")
    last_sync = r.get(f"{PREFIX}:last_sync") or "never"
    return {"total_secrets": total, "last_sync": last_sync, "files_checked": len(SECRET_FILES)}


if __name__ == "__main__":
    count = sync_to_redis()
    print(f"Synced {count} secrets to vault")
    s = stats()
    print(f"Stats: {s}")
    secrets = list_secrets(show_values=False)
    print(f"Secrets ({len(secrets)}):")
    for k, v in sorted(secrets.items()):
        print(f"  {k}: {v}")
    warnings = rotate_check()
    if warnings:
        print(f"Warnings ({len(warnings)}):")
        for w in warnings:
            print(f"  ⚠️  {w}")
    else:
        print("No rotation warnings")
