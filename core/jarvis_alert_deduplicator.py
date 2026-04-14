#!/usr/bin/env python3
"""
jarvis_alert_deduplicator — Deduplicates and suppresses repeated alerts
Groups similar alerts, applies cooldown windows, prevents alert storms
"""

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.alert_deduplicator")

REDIS_PREFIX = "jarvis:alertdedup:"


class AlertSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    RESOLVED = "resolved"


class SuppressReason(str, Enum):
    COOLDOWN = "cooldown"
    DUPLICATE = "duplicate"
    RATE_LIMIT = "rate_limit"
    STORM = "storm"
    SILENCED = "silenced"


@dataclass
class Alert:
    alert_id: str
    source: str
    category: str
    title: str
    body: str
    severity: AlertSeverity = AlertSeverity.WARNING
    labels: dict[str, str] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    @property
    def fingerprint(self) -> str:
        """Deterministic fingerprint based on source + category + title."""
        key = f"{self.source}:{self.category}:{self.title}"
        return hashlib.md5(key.encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        return {
            "alert_id": self.alert_id,
            "source": self.source,
            "category": self.category,
            "title": self.title,
            "body": self.body[:200],
            "severity": self.severity.value,
            "labels": self.labels,
            "fingerprint": self.fingerprint,
            "ts": self.ts,
        }


@dataclass
class DeduplicationPolicy:
    name: str
    cooldown_s: float = 300.0  # suppress same fingerprint for this long
    max_per_window: int = 5  # max alerts of same type per window
    window_s: float = 60.0  # rate limit window
    storm_threshold: int = 20  # total alerts/min before storm suppression
    storm_cooldown_s: float = 120.0
    merge_body: bool = True  # merge repeated alert bodies
    resolve_clears_cooldown: bool = True


@dataclass
class AlertRecord:
    fingerprint: str
    first_seen: float
    last_seen: float
    count: int
    suppressed_count: int
    last_alert: Alert
    cooldown_until: float = 0.0
    silenced_until: float = 0.0

    @property
    def in_cooldown(self) -> bool:
        return time.time() < self.cooldown_until

    @property
    def is_silenced(self) -> bool:
        return time.time() < self.silenced_until

    def to_dict(self) -> dict:
        return {
            "fingerprint": self.fingerprint,
            "count": self.count,
            "suppressed": self.suppressed_count,
            "in_cooldown": self.in_cooldown,
            "cooldown_remaining_s": max(0, round(self.cooldown_until - time.time(), 1)),
            "last_seen": self.last_seen,
        }


@dataclass
class DeduplicationResult:
    alert: Alert
    allowed: bool
    reason: SuppressReason | None
    record: AlertRecord
    merged_count: int = 1

    def to_dict(self) -> dict:
        return {
            "alert_id": self.alert.alert_id,
            "fingerprint": self.alert.fingerprint,
            "allowed": self.allowed,
            "reason": self.reason.value if self.reason else None,
            "occurrence": self.record.count,
        }


class AlertDeduplicator:
    def __init__(self, policy: DeduplicationPolicy | None = None):
        self.redis: aioredis.Redis | None = None
        self._policy = policy or DeduplicationPolicy(name="default")
        self._records: dict[str, AlertRecord] = {}
        self._window_counts: dict[str, list[float]] = {}  # fingerprint → timestamps
        self._global_window: list[float] = []  # all alert timestamps (storm detection)
        self._silenced: dict[str, float] = {}  # fingerprint → silenced until
        self._sink_callbacks: list[Callable] = []
        self._stats: dict[str, int] = {
            "received": 0,
            "allowed": 0,
            "suppressed_cooldown": 0,
            "suppressed_duplicate": 0,
            "suppressed_storm": 0,
            "suppressed_rate": 0,
            "suppressed_silenced": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def set_policy(self, policy: DeduplicationPolicy):
        self._policy = policy

    def add_sink(self, callback: Callable):
        """Register a callback for allowed alerts."""
        self._sink_callbacks.append(callback)

    def silence(self, fingerprint: str, duration_s: float):
        self._silenced[fingerprint] = time.time() + duration_s

    def unsilence(self, fingerprint: str):
        self._silenced.pop(fingerprint, None)

    def _prune_window(self, key: str) -> list[float]:
        cutoff = time.time() - self._policy.window_s
        pruned = [t for t in self._window_counts.get(key, []) if t >= cutoff]
        self._window_counts[key] = pruned
        return pruned

    def _prune_global(self) -> list[float]:
        cutoff = time.time() - 60.0
        self._global_window = [t for t in self._global_window if t >= cutoff]
        return self._global_window

    def _is_storm(self) -> bool:
        return len(self._prune_global()) >= self._policy.storm_threshold

    def process(self, alert: Alert) -> DeduplicationResult:
        self._stats["received"] += 1
        fp = alert.fingerprint
        policy = self._policy
        now = time.time()

        # Get or create record
        if fp not in self._records:
            self._records[fp] = AlertRecord(
                fingerprint=fp,
                first_seen=now,
                last_seen=now,
                count=0,
                suppressed_count=0,
                last_alert=alert,
            )
        record = self._records[fp]
        record.count += 1
        record.last_seen = now
        record.last_alert = alert

        # Silenced check
        silenced_until = self._silenced.get(fp, 0.0)
        if now < silenced_until:
            record.suppressed_count += 1
            self._stats["suppressed_silenced"] += 1
            return DeduplicationResult(alert, False, SuppressReason.SILENCED, record)

        # Resolve clears cooldown
        if alert.severity == AlertSeverity.RESOLVED and policy.resolve_clears_cooldown:
            record.cooldown_until = 0.0

        # Cooldown check
        if record.in_cooldown:
            record.suppressed_count += 1
            self._stats["suppressed_cooldown"] += 1
            return DeduplicationResult(alert, False, SuppressReason.COOLDOWN, record)

        # Storm check
        if self._is_storm():
            record.suppressed_count += 1
            self._stats["suppressed_storm"] += 1
            return DeduplicationResult(alert, False, SuppressReason.STORM, record)

        # Rate limit check
        window_times = self._prune_window(fp)
        if len(window_times) >= policy.max_per_window:
            record.suppressed_count += 1
            self._stats["suppressed_rate"] += 1
            return DeduplicationResult(alert, False, SuppressReason.RATE_LIMIT, record)

        # Allow — set cooldown and track
        record.cooldown_until = now + policy.cooldown_s
        self._window_counts.setdefault(fp, []).append(now)
        self._global_window.append(now)
        self._stats["allowed"] += 1

        # Fire sinks
        for sink in self._sink_callbacks:
            try:
                sink(alert, record)
            except Exception:
                pass

        if self.redis:
            asyncio.create_task(
                self.redis.setex(
                    f"{REDIS_PREFIX}{fp}",
                    int(policy.cooldown_s) + 60,
                    json.dumps(record.to_dict()),
                )
            )

        merged = record.count
        return DeduplicationResult(alert, True, None, record, merged_count=merged)

    def list_records(self) -> list[dict]:
        return [r.to_dict() for r in self._records.values()]

    def active_fingerprints(self) -> list[str]:
        return [fp for fp, r in self._records.items() if r.in_cooldown]

    def stats(self) -> dict:
        total_suppressed = sum(
            v for k, v in self._stats.items() if k.startswith("suppressed")
        )
        return {
            **self._stats,
            "total_suppressed": total_suppressed,
            "active_fingerprints": len(self.active_fingerprints()),
            "total_records": len(self._records),
        }


def build_jarvis_deduplicator() -> AlertDeduplicator:
    policy = DeduplicationPolicy(
        name="jarvis_default",
        cooldown_s=180.0,
        max_per_window=3,
        window_s=60.0,
        storm_threshold=15,
        storm_cooldown_s=120.0,
    )
    dedup = AlertDeduplicator(policy)

    def log_alert(alert: Alert, record: AlertRecord):
        log.warning(
            f"ALERT [{alert.severity.value}] {alert.source}/{alert.category}: {alert.title} (#{record.count})"
        )

    dedup.add_sink(log_alert)
    return dedup


async def main():
    import sys
    import uuid

    dedup = build_jarvis_deduplicator()
    await dedup.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Sending 10 identical alerts...")
        results = {"allowed": 0, "suppressed": 0}
        for i in range(10):
            alert = Alert(
                alert_id=str(uuid.uuid4())[:8],
                source="m1",
                category="gpu_temp",
                title="GPU temperature high",
                body=f"GPU0 temperature is 87°C (attempt {i + 1})",
                severity=AlertSeverity.WARNING,
            )
            result = dedup.process(alert)
            if result.allowed:
                results["allowed"] += 1
                print(f"  ✅ [{i + 1}] ALLOWED  occurrence=#{result.record.count}")
            else:
                results["suppressed"] += 1
                print(
                    f"  🔕 [{i + 1}] {result.reason.value:<15} cooldown={result.record.to_dict()['cooldown_remaining_s']:.0f}s"
                )

        # Send resolved
        resolved = Alert(
            alert_id=str(uuid.uuid4())[:8],
            source="m1",
            category="gpu_temp",
            title="GPU temperature high",
            body="GPU0 temperature back to normal",
            severity=AlertSeverity.RESOLVED,
        )
        r = dedup.process(resolved)
        print(f"\n  RESOLVED: allowed={r.allowed}")

        print(f"\nStats: {json.dumps(dedup.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(dedup.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
