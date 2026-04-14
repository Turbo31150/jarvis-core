#!/usr/bin/env python3
"""
jarvis_alerting_engine — Rule-based alerting with cooldowns and escalation
Evaluates metric thresholds, fires alerts, manages silencing and escalation chains
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.alerting_engine")

REDIS_PREFIX = "jarvis:alert:"
ALERT_LOG = Path("/home/turbo/IA/Core/jarvis/data/alerts.jsonl")


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    EMERGENCY = "emergency"


class AlertState(str, Enum):
    PENDING = "pending"
    FIRING = "firing"
    RESOLVED = "resolved"
    SILENCED = "silenced"


@dataclass
class AlertRule:
    name: str
    metric: str  # metric key to evaluate
    condition: str  # "gt", "lt", "gte", "lte", "eq", "ne"
    threshold: float
    severity: Severity = Severity.WARNING
    cooldown_s: float = 300.0  # minimum seconds between repeated alerts
    for_s: float = 0.0  # metric must be outside threshold for this long
    labels: dict = field(default_factory=dict)
    description: str = ""

    def evaluate(self, value: float) -> bool:
        ops = {
            "gt": value > self.threshold,
            "lt": value < self.threshold,
            "gte": value >= self.threshold,
            "lte": value <= self.threshold,
            "eq": value == self.threshold,
            "ne": value != self.threshold,
        }
        return ops.get(self.condition, False)


@dataclass
class Alert:
    alert_id: str
    rule_name: str
    metric: str
    value: float
    threshold: float
    severity: Severity
    state: AlertState = AlertState.FIRING
    labels: dict = field(default_factory=dict)
    description: str = ""
    fired_at: float = field(default_factory=time.time)
    resolved_at: float = 0.0
    notified: bool = False

    @property
    def duration_s(self) -> float:
        end = self.resolved_at or time.time()
        return round(end - self.fired_at, 1)

    def to_dict(self) -> dict:
        return {
            "alert_id": self.alert_id,
            "rule_name": self.rule_name,
            "metric": self.metric,
            "value": self.value,
            "threshold": self.threshold,
            "severity": self.severity.value,
            "state": self.state.value,
            "labels": self.labels,
            "description": self.description,
            "fired_at": self.fired_at,
            "resolved_at": self.resolved_at,
            "duration_s": self.duration_s,
        }


class AlertingEngine:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._rules: dict[str, AlertRule] = {}
        self._active: dict[str, Alert] = {}  # rule_name → active alert
        self._history: list[Alert] = []
        self._last_fired: dict[str, float] = {}  # rule_name → last fire ts
        self._pending_since: dict[str, float] = {}  # rule_name → first violation ts
        self._silences: dict[str, float] = {}  # rule_name → silence until ts
        self._handlers: dict[Severity, list[Callable]] = {s: [] for s in Severity}
        ALERT_LOG.parent.mkdir(parents=True, exist_ok=True)

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def add_rule(self, rule: AlertRule):
        self._rules[rule.name] = rule
        log.debug(
            f"Alert rule added: {rule.name} ({rule.metric} {rule.condition} {rule.threshold})"
        )

    def on_alert(self, severity: Severity, handler: Callable):
        """Register handler: handler(alert: Alert)"""
        self._handlers[severity].append(handler)

    def on_any_alert(self, handler: Callable):
        for sev in Severity:
            self._handlers[sev].append(handler)

    def silence(self, rule_name: str, duration_s: float = 3600.0):
        self._silences[rule_name] = time.time() + duration_s
        log.info(f"Alert silenced: {rule_name} for {duration_s}s")

    def unsilence(self, rule_name: str):
        self._silences.pop(rule_name, None)

    def is_silenced(self, rule_name: str) -> bool:
        until = self._silences.get(rule_name, 0)
        if until and time.time() < until:
            return True
        self._silences.pop(rule_name, None)
        return False

    async def evaluate(
        self, metric: str, value: float, labels: dict | None = None
    ) -> list[Alert]:
        fired = []
        now = time.time()

        for rule in self._rules.values():
            if rule.metric != metric:
                continue

            violating = rule.evaluate(value)

            if violating:
                # Track pending duration
                if rule.name not in self._pending_since:
                    self._pending_since[rule.name] = now

                pending_duration = now - self._pending_since[rule.name]
                if pending_duration < rule.for_s:
                    continue  # Not yet sustained long enough

                # Check cooldown
                last = self._last_fired.get(rule.name, 0)
                if now - last < rule.cooldown_s:
                    continue

                # Check silence
                if self.is_silenced(rule.name):
                    continue

                # Fire alert
                import uuid

                alert = Alert(
                    alert_id=str(uuid.uuid4())[:8],
                    rule_name=rule.name,
                    metric=metric,
                    value=value,
                    threshold=rule.threshold,
                    severity=rule.severity,
                    labels=labels or {},
                    description=rule.description
                    or f"{metric} {rule.condition} {rule.threshold} (value={value})",
                )
                self._active[rule.name] = alert
                self._last_fired[rule.name] = now
                self._history.append(alert)
                fired.append(alert)

                self._persist(alert)
                await self._notify(alert)

                log.warning(
                    f"ALERT [{rule.severity.value}] {rule.name}: {alert.description}"
                )

            else:
                # Resolve if was active
                self._pending_since.pop(rule.name, None)
                if rule.name in self._active:
                    active = self._active.pop(rule.name)
                    active.state = AlertState.RESOLVED
                    active.resolved_at = now
                    self._persist(active)
                    log.info(f"Alert resolved: {rule.name} after {active.duration_s}s")

        return fired

    async def evaluate_all(
        self, metrics: dict[str, float], labels: dict | None = None
    ) -> list[Alert]:
        all_fired = []
        for metric, value in metrics.items():
            alerts = await self.evaluate(metric, value, labels)
            all_fired.extend(alerts)
        return all_fired

    async def _notify(self, alert: Alert):
        handlers = self._handlers.get(alert.severity, [])
        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(alert)
                else:
                    handler(alert)
            except Exception as e:
                log.debug(f"Alert handler error: {e}")

        if self.redis:
            await self.redis.publish(
                "jarvis:events",
                json.dumps({"event": "alert", **alert.to_dict()}),
            )
            await self.redis.lpush(f"{REDIS_PREFIX}active", json.dumps(alert.to_dict()))
            await self.redis.ltrim(f"{REDIS_PREFIX}active", 0, 999)

    def _persist(self, alert: Alert):
        with open(ALERT_LOG, "a") as f:
            f.write(json.dumps(alert.to_dict()) + "\n")

    def active_alerts(self) -> list[dict]:
        return [a.to_dict() for a in self._active.values()]

    def recent_history(self, limit: int = 20) -> list[dict]:
        return [a.to_dict() for a in reversed(self._history[-limit:])]

    def stats(self) -> dict:
        return {
            "rules": len(self._rules),
            "active": len(self._active),
            "silenced": len(self._silences),
            "total_fired": len(self._history),
            "by_severity": {
                sev.value: sum(1 for a in self._history if a.severity == sev)
                for sev in Severity
            },
        }


# Pre-built JARVIS rules
def default_rules() -> list[AlertRule]:
    return [
        AlertRule(
            "gpu_temp_high", "gpu_temp_c", "gt", 83.0, Severity.WARNING, cooldown_s=300
        ),
        AlertRule(
            "gpu_temp_critical",
            "gpu_temp_c",
            "gt",
            89.0,
            Severity.CRITICAL,
            cooldown_s=60,
        ),
        AlertRule("ram_high", "ram_pct", "gt", 85.0, Severity.WARNING, cooldown_s=300),
        AlertRule(
            "ram_critical", "ram_pct", "gt", 95.0, Severity.CRITICAL, cooldown_s=60
        ),
        AlertRule(
            "error_rate_high",
            "llm_error_rate",
            "gt",
            0.1,
            Severity.WARNING,
            cooldown_s=120,
        ),
        AlertRule(
            "latency_high",
            "llm_latency_p95_ms",
            "gt",
            10000.0,
            Severity.WARNING,
            cooldown_s=300,
        ),
        AlertRule(
            "disk_high", "disk_pct", "gt", 85.0, Severity.WARNING, cooldown_s=3600
        ),
        AlertRule(
            "disk_critical", "disk_pct", "gt", 95.0, Severity.CRITICAL, cooldown_s=300
        ),
    ]


async def main():
    import sys

    engine = AlertingEngine()
    for rule in default_rules():
        engine.add_rule(rule)

    def print_alert(alert: Alert):
        print(
            f"  🔔 [{alert.severity.value.upper()}] {alert.rule_name}: {alert.description}"
        )

    engine.on_any_alert(print_alert)
    await engine.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Evaluating metrics...")
        await engine.evaluate_all(
            {
                "gpu_temp_c": 91.0,
                "ram_pct": 88.0,
                "llm_error_rate": 0.05,
                "latency_p95_ms": 5000.0,
            }
        )
        print(f"\nActive alerts: {len(engine.active_alerts())}")
        print(f"Stats: {json.dumps(engine.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(engine.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
