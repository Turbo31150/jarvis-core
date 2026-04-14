#!/usr/bin/env python3
"""
jarvis_event_deduplicator — Event deduplication with fingerprinting and TTL windows
Prevents duplicate alerts, messages, and tasks from flooding the system
"""

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.event_deduplicator")

REDIS_PREFIX = "jarvis:dedup:"


class DedupeStrategy(str, Enum):
    EXACT = "exact"  # hash entire payload
    KEY_ONLY = "key_only"  # hash only specified key fields
    FUZZY = "fuzzy"  # ignore numeric values, hash structure


class DedupeAction(str, Enum):
    PASS = "pass"  # first occurrence — allow
    SUPPRESS = "suppress"  # duplicate — block
    MERGE = "merge"  # duplicate but update count/ts


@dataclass
class DedupeRecord:
    fingerprint: str
    event_type: str
    first_seen: float
    last_seen: float
    count: int = 1
    suppressed: int = 0
    payload_sample: dict = field(default_factory=dict)
    ttl_s: float = 300.0
    tags: list[str] = field(default_factory=list)

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.last_seen) > self.ttl_s

    def to_dict(self) -> dict:
        return {
            "fingerprint": self.fingerprint,
            "event_type": self.event_type,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "count": self.count,
            "suppressed": self.suppressed,
            "age_s": round(time.time() - self.first_seen, 1),
            "tags": self.tags,
        }


@dataclass
class DedupeResult:
    action: DedupeAction
    fingerprint: str
    record: DedupeRecord
    is_new: bool

    def to_dict(self) -> dict:
        return {
            "action": self.action.value,
            "fingerprint": self.fingerprint,
            "is_new": self.is_new,
            "count": self.record.count,
            "suppressed": self.record.suppressed,
        }


@dataclass
class DedupeRule:
    rule_id: str
    event_type: str  # exact or prefix match ("alert.*")
    strategy: DedupeStrategy
    ttl_s: float = 300.0
    key_fields: list[str] = field(default_factory=list)  # for KEY_ONLY strategy
    max_count: int = 0  # 0 = suppress all duplicates after first
    tags: list[str] = field(default_factory=list)


def _fingerprint_exact(event_type: str, payload: dict) -> str:
    raw = json.dumps({"type": event_type, **payload}, sort_keys=True)
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def _fingerprint_key_only(event_type: str, payload: dict, keys: list[str]) -> str:
    filtered = {k: payload.get(k) for k in sorted(keys)}
    raw = json.dumps({"type": event_type, **filtered}, sort_keys=True)
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def _fingerprint_fuzzy(event_type: str, payload: dict) -> str:
    """Hash structure but ignore numeric leaf values."""

    def _normalize(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: _normalize(v) for k, v in sorted(obj.items())}
        elif isinstance(obj, (int, float)):
            return "__NUM__"
        elif isinstance(obj, list):
            return [_normalize(i) for i in obj]
        return obj

    raw = json.dumps({"type": event_type, **_normalize(payload)}, sort_keys=True)
    return hashlib.md5(raw.encode()).hexdigest()[:16]


class EventDeduplicator:
    def __init__(self, default_ttl_s: float = 300.0):
        self.redis: aioredis.Redis | None = None
        self._rules: list[DedupeRule] = []
        self._records: dict[str, DedupeRecord] = {}  # fingerprint → record
        self._default_ttl = default_ttl_s
        self._stats: dict[str, int] = {
            "checked": 0,
            "passed": 0,
            "suppressed": 0,
            "expired_purged": 0,
        }
        self._purge_counter = 0

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def add_rule(self, rule: DedupeRule):
        self._rules.append(rule)

    def _match_rule(self, event_type: str) -> DedupeRule | None:
        for rule in self._rules:
            pattern = rule.event_type
            if pattern.endswith("*"):
                if event_type.startswith(pattern[:-1]):
                    return rule
            elif event_type == pattern or pattern == "*":
                return rule
        return None

    def _make_fingerprint(
        self, event_type: str, payload: dict, rule: DedupeRule | None
    ) -> str:
        if not rule:
            return _fingerprint_key_only(event_type, payload, list(payload.keys())[:5])
        if rule.strategy == DedupeStrategy.EXACT:
            return _fingerprint_exact(event_type, payload)
        elif rule.strategy == DedupeStrategy.KEY_ONLY:
            keys = rule.key_fields or list(payload.keys())[:5]
            return _fingerprint_key_only(event_type, payload, keys)
        else:
            return _fingerprint_fuzzy(event_type, payload)

    def check(
        self,
        event_type: str,
        payload: dict,
        tags: list[str] | None = None,
    ) -> DedupeResult:
        self._stats["checked"] += 1
        rule = self._match_rule(event_type)
        fingerprint = self._make_fingerprint(event_type, payload, rule)
        ttl = rule.ttl_s if rule else self._default_ttl

        now = time.time()
        record = self._records.get(fingerprint)

        # Periodic purge
        self._purge_counter += 1
        if self._purge_counter >= 100:
            self._purge_expired()
            self._purge_counter = 0

        if record is None or record.is_expired:
            # New event
            record = DedupeRecord(
                fingerprint=fingerprint,
                event_type=event_type,
                first_seen=now,
                last_seen=now,
                ttl_s=ttl,
                payload_sample=dict(list(payload.items())[:10]),
                tags=tags or [],
            )
            self._records[fingerprint] = record
            self._stats["passed"] += 1

            if self.redis:
                asyncio.create_task(self._redis_set(fingerprint, ttl))

            return DedupeResult(
                action=DedupeAction.PASS,
                fingerprint=fingerprint,
                record=record,
                is_new=True,
            )

        # Existing record — check if we should suppress or merge
        max_count = rule.max_count if rule else 0
        record.last_seen = now
        record.count += 1

        if max_count > 0 and record.count <= max_count:
            # Allow up to max_count occurrences
            self._stats["passed"] += 1
            return DedupeResult(
                action=DedupeAction.PASS,
                fingerprint=fingerprint,
                record=record,
                is_new=False,
            )

        # Suppress
        record.suppressed += 1
        self._stats["suppressed"] += 1
        log.debug(
            f"Suppressed duplicate [{event_type}] fp={fingerprint} "
            f"count={record.count} suppressed={record.suppressed}"
        )

        return DedupeResult(
            action=DedupeAction.SUPPRESS,
            fingerprint=fingerprint,
            record=record,
            is_new=False,
        )

    def allow(
        self, event_type: str, payload: dict, tags: list[str] | None = None
    ) -> bool:
        return self.check(event_type, payload, tags).action != DedupeAction.SUPPRESS

    def reset(self, fingerprint: str):
        """Force-expire a record so the next event is treated as new."""
        self._records.pop(fingerprint, None)
        if self.redis:
            asyncio.create_task(self._redis_del(fingerprint))

    def _purge_expired(self):
        expired = [fp for fp, r in self._records.items() if r.is_expired]
        for fp in expired:
            del self._records[fp]
        self._stats["expired_purged"] += len(expired)

    async def _redis_set(self, fingerprint: str, ttl_s: float):
        if not self.redis:
            return
        try:
            await self.redis.setex(f"{REDIS_PREFIX}{fingerprint}", int(ttl_s), "1")
        except Exception:
            pass

    async def _redis_del(self, fingerprint: str):
        if not self.redis:
            return
        try:
            await self.redis.delete(f"{REDIS_PREFIX}{fingerprint}")
        except Exception:
            pass

    def active_records(self) -> list[dict]:
        return [r.to_dict() for r in self._records.values() if not r.is_expired]

    def top_suppressed(self, limit: int = 10) -> list[dict]:
        return sorted(
            [r.to_dict() for r in self._records.values()],
            key=lambda r: -r["suppressed"],
        )[:limit]

    def stats(self) -> dict:
        active = sum(1 for r in self._records.values() if not r.is_expired)
        return {
            **self._stats,
            "active_fingerprints": active,
            "total_fingerprints": len(self._records),
            "suppress_rate": round(
                self._stats["suppressed"] / max(self._stats["checked"], 1), 4
            ),
        }


def build_jarvis_event_deduplicator() -> EventDeduplicator:
    dedup = EventDeduplicator(default_ttl_s=300.0)

    dedup.add_rule(
        DedupeRule(
            rule_id="alert-dedup",
            event_type="alert.*",
            strategy=DedupeStrategy.KEY_ONLY,
            key_fields=["severity", "source", "message"],
            ttl_s=300.0,
        )
    )
    dedup.add_rule(
        DedupeRule(
            rule_id="error-dedup",
            event_type="error",
            strategy=DedupeStrategy.FUZZY,
            ttl_s=60.0,
        )
    )
    dedup.add_rule(
        DedupeRule(
            rule_id="metric-spike",
            event_type="metric.spike",
            strategy=DedupeStrategy.KEY_ONLY,
            key_fields=["metric", "node"],
            ttl_s=120.0,
            max_count=2,
        )
    )

    return dedup


async def main():
    import sys

    dedup = build_jarvis_event_deduplicator()
    await dedup.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Event deduplicator demo...")

        events = [
            (
                "alert.gpu",
                {
                    "severity": "warning",
                    "source": "m1",
                    "message": "GPU temp high",
                    "value": 81,
                },
            ),
            (
                "alert.gpu",
                {
                    "severity": "warning",
                    "source": "m1",
                    "message": "GPU temp high",
                    "value": 83,
                },
            ),
            (
                "alert.gpu",
                {
                    "severity": "warning",
                    "source": "m1",
                    "message": "GPU temp high",
                    "value": 85,
                },
            ),
            (
                "alert.gpu",
                {
                    "severity": "critical",
                    "source": "m1",
                    "message": "GPU overheat",
                    "value": 91,
                },
            ),
            ("error", {"code": 500, "endpoint": "/v1/chat", "latency": 5001}),
            ("error", {"code": 500, "endpoint": "/v1/chat", "latency": 4998}),
            ("metric.spike", {"metric": "cpu_pct", "node": "m2", "value": 95}),
            ("metric.spike", {"metric": "cpu_pct", "node": "m2", "value": 96}),
            ("metric.spike", {"metric": "cpu_pct", "node": "m2", "value": 97}),
        ]

        for event_type, payload in events:
            result = dedup.check(event_type, payload)
            icon = {"pass": "✅", "suppress": "🔇", "merge": "🔄"}.get(
                result.action.value, "?"
            )
            print(
                f"  {icon} {event_type:<20} fp={result.fingerprint} count={result.record.count} suppressed={result.record.suppressed}"
            )

        print(f"\nStats: {json.dumps(dedup.stats(), indent=2)}")
        print("\nTop suppressed:")
        for r in dedup.top_suppressed(5):
            print(
                f"  {r['event_type']:<20} suppressed={r['suppressed']} count={r['count']}"
            )

    elif cmd == "stats":
        print(json.dumps(dedup.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
