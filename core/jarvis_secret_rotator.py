#!/usr/bin/env python3
"""
jarvis_secret_rotator — API key rotation, expiry tracking, secure storage
Manages API keys lifecycle: store, rotate, audit, alert on expiry
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.secret_rotator")

SECRETS_FILE = Path("/home/turbo/IA/Core/jarvis/data/.secrets_vault.json")
REDIS_PREFIX = "jarvis:secret:"
AUDIT_KEY = "jarvis:secret:audit"
EXPIRY_WARN_DAYS = 14


@dataclass
class SecretEntry:
    name: str
    value_enc: str  # base64-encoded obfuscated value
    category: str  # api_key | token | password | cert
    owner: str = "jarvis"
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0  # 0 = never
    last_used: float = 0.0
    rotation_count: int = 0
    tags: list[str] = field(default_factory=list)
    checksum: str = ""  # sha256 of raw value for integrity

    @property
    def is_expired(self) -> bool:
        return self.expires_at > 0 and time.time() > self.expires_at

    @property
    def days_until_expiry(self) -> float | None:
        if self.expires_at == 0:
            return None
        return round((self.expires_at - time.time()) / 86400, 1)

    @property
    def expiry_warning(self) -> bool:
        d = self.days_until_expiry
        return d is not None and 0 < d < EXPIRY_WARN_DAYS

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "value_enc": self.value_enc,
            "category": self.category,
            "owner": self.owner,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "last_used": self.last_used,
            "rotation_count": self.rotation_count,
            "tags": self.tags,
            "checksum": self.checksum,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SecretEntry":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def _obfuscate(value: str) -> str:
    """Simple XOR obfuscation with env-derived key (not encryption — use vault for prod)."""
    key = os.environ.get("JARVIS_VAULT_KEY", "jarvis-default-key-2026")
    key_bytes = key.encode() * (len(value) // len(key) + 1)
    xored = bytes(ord(c) ^ key_bytes[i] for i, c in enumerate(value))
    return base64.b64encode(xored).decode()


def _deobfuscate(value_enc: str) -> str:
    key = os.environ.get("JARVIS_VAULT_KEY", "jarvis-default-key-2026")
    raw = base64.b64decode(value_enc)
    key_bytes = key.encode() * (len(raw) // len(key) + 1)
    return "".join(chr(b ^ key_bytes[i]) for i, b in enumerate(raw))


def _checksum(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


class SecretRotator:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._secrets: dict[str, SecretEntry] = {}
        self._load()

    def _load(self):
        if SECRETS_FILE.exists():
            try:
                data = json.loads(SECRETS_FILE.read_text())
                for name, d in data.items():
                    self._secrets[name] = SecretEntry.from_dict(d)
            except Exception as e:
                log.warning(f"Load secrets error: {e}")

    def _save(self):
        SECRETS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SECRETS_FILE.chmod(0o600)
        SECRETS_FILE.write_text(
            json.dumps({k: v.to_dict() for k, v in self._secrets.items()}, indent=2)
        )
        SECRETS_FILE.chmod(0o600)

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    async def _audit(self, action: str, name: str, extra: str = ""):
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "action": action,
            "name": name,
            "extra": extra,
        }
        log.info(f"[AUDIT] {action} secret '{name}' {extra}")
        if self.redis:
            await self.redis.lpush(AUDIT_KEY, json.dumps(entry))
            await self.redis.ltrim(AUDIT_KEY, 0, 999)

    def store(
        self,
        name: str,
        value: str,
        category: str = "api_key",
        expires_days: int = 0,
        tags: list[str] | None = None,
    ) -> SecretEntry:
        enc = _obfuscate(value)
        entry = SecretEntry(
            name=name,
            value_enc=enc,
            category=category,
            expires_at=time.time() + expires_days * 86400 if expires_days > 0 else 0.0,
            tags=tags or [],
            checksum=_checksum(value),
        )
        self._secrets[name] = entry
        self._save()
        return entry

    def get(self, name: str) -> str | None:
        entry = self._secrets.get(name)
        if not entry:
            return None
        if entry.is_expired:
            log.warning(f"Secret '{name}' is EXPIRED")
            return None
        entry.last_used = time.time()
        self._save()
        return _deobfuscate(entry.value_enc)

    async def rotate(self, name: str, new_value: str) -> bool:
        entry = self._secrets.get(name)
        if not entry:
            return False
        old_checksum = entry.checksum
        entry.value_enc = _obfuscate(new_value)
        entry.checksum = _checksum(new_value)
        entry.rotation_count += 1
        entry.created_at = time.time()
        self._save()
        await self._audit("rotate", name, f"rotation #{entry.rotation_count}")
        log.info(f"Rotated secret '{name}' (rotation #{entry.rotation_count})")
        return True

    def delete(self, name: str) -> bool:
        if name not in self._secrets:
            return False
        del self._secrets[name]
        self._save()
        return True

    def list_secrets(self, category: str | None = None) -> list[dict]:
        result = []
        for entry in self._secrets.values():
            if category and entry.category != category:
                continue
            d = entry.days_until_expiry
            result.append(
                {
                    "name": entry.name,
                    "category": entry.category,
                    "tags": entry.tags,
                    "expires": f"in {d:.0f}d" if d is not None else "never",
                    "expired": entry.is_expired,
                    "warning": entry.expiry_warning,
                    "rotations": entry.rotation_count,
                    "last_used": time.strftime(
                        "%Y-%m-%d", time.localtime(entry.last_used)
                    )
                    if entry.last_used
                    else "never",
                }
            )
        return sorted(result, key=lambda x: x["name"])

    async def check_expiry(self) -> list[dict]:
        alerts = []
        for entry in self._secrets.values():
            if entry.is_expired:
                alerts.append({"name": entry.name, "status": "EXPIRED"})
                await self._audit("expiry_alert", entry.name, "EXPIRED")
            elif entry.expiry_warning:
                d = entry.days_until_expiry
                alerts.append({"name": entry.name, "status": f"EXPIRING in {d:.0f}d"})

        if alerts and self.redis:
            await self.redis.publish(
                "jarvis:events",
                json.dumps({"event": "secret_expiry_alert", "alerts": alerts}),
            )
        return alerts

    async def audit_log(self, limit: int = 20) -> list[dict]:
        if not self.redis:
            return []
        items = await self.redis.lrange(AUDIT_KEY, 0, limit - 1)
        return [json.loads(i) for i in items]


# ── CLI ───────────────────────────────────────────────────────────────────────


async def main():
    import sys

    rotator = SecretRotator()
    await rotator.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"

    if cmd == "list":
        secrets = rotator.list_secrets()
        if not secrets:
            print("No secrets stored")
        else:
            print(
                f"{'Name':<28} {'Category':<12} {'Expires':>12} {'Rotations':>10} {'Last used'}"
            )
            print("-" * 80)
            for s in secrets:
                warn = " ⚠️" if s["warning"] else ""
                exp = " ❌" if s["expired"] else ""
                print(
                    f"{s['name']:<28} {s['category']:<12} {s['expires']:>12}{warn}{exp} "
                    f"{s['rotations']:>10}  {s['last_used']}"
                )

    elif cmd == "store" and len(sys.argv) > 3:
        name, value = sys.argv[2], sys.argv[3]
        cat = sys.argv[4] if len(sys.argv) > 4 else "api_key"
        days = int(sys.argv[5]) if len(sys.argv) > 5 else 0
        rotator.store(name, value, category=cat, expires_days=days)
        print(f"Stored: {name}")

    elif cmd == "get" and len(sys.argv) > 2:
        val = rotator.get(sys.argv[2])
        if val:
            print(f"{sys.argv[2]}: {val[:4]}...{val[-4:]}")
        else:
            print(f"Not found or expired: {sys.argv[2]}")

    elif cmd == "rotate" and len(sys.argv) > 3:
        ok = await rotator.rotate(sys.argv[2], sys.argv[3])
        print(f"Rotated {sys.argv[2]}: {ok}")

    elif cmd == "check":
        alerts = await rotator.check_expiry()
        if not alerts:
            print("All secrets OK")
        else:
            for a in alerts:
                print(f"  ⚠️  {a['name']}: {a['status']}")

    elif cmd == "audit":
        log_entries = await rotator.audit_log()
        for e in log_entries:
            print(f"  [{e['ts']}] {e['action']:<12} {e['name']} {e.get('extra', '')}")


if __name__ == "__main__":
    asyncio.run(main())
