#!/usr/bin/env python3
"""
jarvis_alert_escalator — Multi-level alert escalation with suppression and routing
Deduplicate, group, and escalate alerts through configurable channels with backoff
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

log = logging.getLogger("jarvis.alert_escalator")

REDIS_PREFIX = "jarvis:alerts:"


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"
    FATAL = "fatal"


class EscalationState(str, Enum):
    NEW = "new"
    ACKNOWLEDGED = "acknowledged"
    ESCALATED = "escalated"
    RESOLVED = "resolved"
    SUPPRESSED = "suppressed"


_SEVERITY_LEVEL = {
    Severity.INFO: 0,
    Severity.WARNING: 1,
    Severity.ERROR: 2,
    Severity.CRITICAL: 3,
    Severity.FATAL: 4,
}


@dataclass
class Alert:
    alert_id: str
    title: str
    body: str
    severity: Severity
    source: str
    tags: list[str] = field(default_factory=list)
    state: EscalationState = EscalationState.NEW
    count: int = 1  # occurrences since first seen
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    escalated_at: float = 0.0
    resolved_at: float = 0.0
    fingerprint: str = ""  # dedup key

    def to_dict(self) -> dict:
        return {
            "alert_id": self.alert_id,
            "title": self.title,
            "body": self.body[:200],
            "severity": self.severity.value,
            "source": self.source,
            "tags": self.tags,
            "state": self.state.value,
            "count": self.count,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
        }


@dataclass
class EscalationPolicy:
    name: str
    min_severity: Severity = Severity.ERROR
    escalate_after_s: float = 300.0  # if not acked, escalate
    max_escalations: int = 3
    suppression_window_s: float = 60.0  # suppress duplicate within window
    channels: list[str] = field(default_factory=list)  # ["telegram", "log", "redis"]


def _fingerprint(title: str, source: str) -> str:
    return hashlib.md5(f"{title}:{source}".encode()).hexdigest()[:12]


class AlertEscalator:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._alerts: dict[str, Alert] = {}  # fingerprint → alert
        self._policies: dict[str, EscalationPolicy] = {}
        self._channel_handlers: dict[str, Callable] = {}
        self._escalation_counts: dict[str, int] = {}  # alert_id → count
        self._stats: dict[str, int] = {
            "received": 0,
            "suppressed": 0,
            "escalated": 0,
            "resolved": 0,
            "deduplicated": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def add_policy(self, policy: EscalationPolicy):
        self._policies[policy.name] = policy

    def register_channel(self, channel: str, handler: Callable):
        """Register a handler for a notification channel."""
        self._channel_handlers[channel] = handler

    def _find_policy(self, alert: Alert) -> EscalationPolicy | None:
        """Find the most specific matching policy."""
        matching = [
            p
            for p in self._policies.values()
            if _SEVERITY_LEVEL[alert.severity] >= _SEVERITY_LEVEL[p.min_severity]
        ]
        if not matching:
            return None
        return max(matching, key=lambda p: _SEVERITY_LEVEL[p.min_severity])

    async def _dispatch(self, alert: Alert, channel: str):
        handler = self._channel_handlers.get(channel)
        if handler:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(alert)
                else:
                    handler(alert)
            except Exception as e:
                log.warning(f"Channel '{channel}' dispatch error: {e}")

    async def fire(self, alert: Alert) -> EscalationState:
        self._stats["received"] += 1
        fp = _fingerprint(alert.title, alert.source)
        alert.fingerprint = fp

        policy = self._find_policy(alert)

        # Deduplication
        existing = self._alerts.get(fp)
        if existing:
            window = policy.suppression_window_s if policy else 60.0
            if (time.time() - existing.last_seen) < window:
                existing.count += 1
                existing.last_seen = time.time()
                self._stats["deduplicated"] += 1
                if existing.state == EscalationState.SUPPRESSED:
                    self._stats["suppressed"] += 1
                return existing.state
            existing.count += 1
            existing.last_seen = time.time()
            self._alerts[fp] = existing
        else:
            alert.alert_id = f"alert-{fp}"
            self._alerts[fp] = alert
            existing = alert

        if not policy:
            log.info(f"Alert (no policy): [{existing.severity.value}] {existing.title}")
            return existing.state

        # Dispatch to channels
        for channel in policy.channels:
            await self._dispatch(existing, channel)

        if self.redis:
            asyncio.create_task(
                self.redis.setex(
                    f"{REDIS_PREFIX}{existing.alert_id}",
                    3600,
                    json.dumps(existing.to_dict()),
                )
            )

        log.info(
            f"Alert fired: [{existing.severity.value}] {existing.title} "
            f"(count={existing.count}, state={existing.state.value})"
        )
        return existing.state

    def acknowledge(self, alert_id: str) -> bool:
        for alert in self._alerts.values():
            if alert.alert_id == alert_id:
                alert.state = EscalationState.ACKNOWLEDGED
                log.info(f"Alert acknowledged: {alert_id}")
                return True
        return False

    def resolve(self, alert_id: str) -> bool:
        for fp, alert in list(self._alerts.items()):
            if alert.alert_id == alert_id:
                alert.state = EscalationState.RESOLVED
                alert.resolved_at = time.time()
                self._stats["resolved"] += 1
                log.info(f"Alert resolved: {alert_id} ({alert.title})")
                return True
        return False

    def suppress(self, fingerprint: str, duration_s: float = 3600.0):
        alert = self._alerts.get(fingerprint)
        if alert:
            alert.state = EscalationState.SUPPRESSED
            self._stats["suppressed"] += 1

    async def check_escalations(self):
        """Called periodically to escalate unacked alerts."""
        now = time.time()
        for alert in self._alerts.values():
            if alert.state not in (EscalationState.NEW,):
                continue
            policy = self._find_policy(alert)
            if not policy:
                continue
            count = self._escalation_counts.get(alert.alert_id, 0)
            if count >= policy.max_escalations:
                continue
            if (now - alert.first_seen) >= policy.escalate_after_s * (count + 1):
                alert.state = EscalationState.ESCALATED
                alert.escalated_at = now
                self._escalation_counts[alert.alert_id] = count + 1
                self._stats["escalated"] += 1
                log.warning(
                    f"Alert ESCALATED (#{count + 1}): [{alert.severity.value}] {alert.title}"
                )
                for channel in policy.channels:
                    await self._dispatch(alert, channel)

    def active_alerts(self, min_severity: Severity | None = None) -> list[dict]:
        result = [
            a.to_dict()
            for a in self._alerts.values()
            if a.state not in (EscalationState.RESOLVED, EscalationState.SUPPRESSED)
        ]
        if min_severity:
            result = [
                a
                for a in result
                if _SEVERITY_LEVEL[Severity(a["severity"])]
                >= _SEVERITY_LEVEL[min_severity]
            ]
        return sorted(result, key=lambda a: -_SEVERITY_LEVEL[Severity(a["severity"])])

    def stats(self) -> dict:
        active = len(
            [
                a
                for a in self._alerts.values()
                if a.state not in (EscalationState.RESOLVED, EscalationState.SUPPRESSED)
            ]
        )
        return {
            **self._stats,
            "active_alerts": active,
            "total_alerts": len(self._alerts),
        }


def build_jarvis_alert_escalator() -> AlertEscalator:
    esc = AlertEscalator()

    esc.add_policy(
        EscalationPolicy(
            name="critical_fast",
            min_severity=Severity.CRITICAL,
            escalate_after_s=60.0,
            max_escalations=3,
            suppression_window_s=30.0,
            channels=["log", "redis"],
        )
    )
    esc.add_policy(
        EscalationPolicy(
            name="error_standard",
            min_severity=Severity.ERROR,
            escalate_after_s=300.0,
            max_escalations=2,
            suppression_window_s=60.0,
            channels=["log"],
        )
    )
    esc.add_policy(
        EscalationPolicy(
            name="warning_slow",
            min_severity=Severity.WARNING,
            escalate_after_s=900.0,
            max_escalations=1,
            suppression_window_s=120.0,
            channels=["log"],
        )
    )

    def log_handler(alert: Alert):
        icon = {
            "info": "ℹ️",
            "warning": "⚠️",
            "error": "❌",
            "critical": "🔴",
            "fatal": "💀",
        }.get(alert.severity.value, "?")
        log.warning(f"{icon} [{alert.source}] {alert.title}: {alert.body[:80]}")

    async def redis_handler(alert: Alert):
        pass  # handled in fire()

    esc.register_channel("log", log_handler)
    esc.register_channel("redis", redis_handler)
    return esc


async def main():
    import sys

    esc = build_jarvis_alert_escalator()
    await esc.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":

        alerts = [
            Alert(
                alert_id="",
                title="GPU temperature critical",
                body="GPU0 reached 92°C, throttling initiated",
                severity=Severity.CRITICAL,
                source="jarvis-gpu",
                tags=["gpu", "thermal"],
            ),
            Alert(
                alert_id="",
                title="Redis connection failed",
                body="Cannot connect to Redis at localhost:6379",
                severity=Severity.ERROR,
                source="jarvis-kv",
                tags=["redis", "connectivity"],
            ),
            Alert(
                alert_id="",
                title="GPU temperature critical",  # duplicate
                body="GPU0 still at 91°C",
                severity=Severity.CRITICAL,
                source="jarvis-gpu",
                tags=["gpu", "thermal"],
            ),
            Alert(
                alert_id="",
                title="Model load slow",
                body="deepseek-r1 took 45s to load",
                severity=Severity.WARNING,
                source="jarvis-model",
                tags=["model", "performance"],
            ),
        ]

        print("Firing alerts...")
        for a in alerts:
            state = await esc.fire(a)
            print(f"  [{a.severity.value:<8}] {a.title[:40]:<40} → {state.value}")

        print("\nActive alerts:")
        for a in esc.active_alerts():
            print(
                f"  [{a['severity']:<8}] {a['title'][:40]:<40} "
                f"count={a['count']} state={a['state']}"
            )

        print(f"\nStats: {json.dumps(esc.stats(), indent=2)}")

    elif cmd == "list":
        for a in esc.active_alerts():
            print(json.dumps(a))

    elif cmd == "stats":
        print(json.dumps(esc.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
