#!/usr/bin/env python3
"""
jarvis_slo_tracker — SLO/SLA compliance tracking with burn rate alerts
Tracks error budgets, burn rates, and multi-window SLO compliance
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.slo_tracker")

REDIS_PREFIX = "jarvis:slo:"
SLO_FILE = Path("/home/turbo/IA/Core/jarvis/data/slo_events.jsonl")


@dataclass
class SLODefinition:
    name: str
    target_pct: float  # e.g. 99.9 = 99.9% success rate
    window_days: int = 30
    latency_p99_ms: float = 0.0  # 0 = no latency SLO
    description: str = ""

    @property
    def error_budget_pct(self) -> float:
        return 100.0 - self.target_pct

    def allowed_failures(self, total_requests: int) -> int:
        return int(total_requests * self.error_budget_pct / 100)


@dataclass
class SLOEvent:
    slo_name: str
    success: bool
    latency_ms: float
    ts: float = field(default_factory=time.time)
    context: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "slo_name": self.slo_name,
            "success": self.success,
            "latency_ms": round(self.latency_ms, 1),
            "ts": self.ts,
            "context": self.context,
        }


@dataclass
class BurnRate:
    window_h: float
    rate: float  # current error rate / allowed error rate
    is_burning: bool  # rate > 1.0

    def to_dict(self) -> dict:
        return {
            "window_h": self.window_h,
            "burn_rate": round(self.rate, 3),
            "is_burning": self.is_burning,
        }


@dataclass
class SLOReport:
    slo_name: str
    target_pct: float
    actual_pct: float
    compliant: bool
    error_budget_used_pct: float
    total_requests: int
    failures: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    burn_rates: list[BurnRate]
    window_days: int

    def to_dict(self) -> dict:
        return {
            "slo_name": self.slo_name,
            "target_pct": self.target_pct,
            "actual_pct": round(self.actual_pct, 4),
            "compliant": self.compliant,
            "error_budget_used_pct": round(self.error_budget_used_pct, 2),
            "total_requests": self.total_requests,
            "failures": self.failures,
            "latency": {
                "p50_ms": round(self.p50_ms, 1),
                "p95_ms": round(self.p95_ms, 1),
                "p99_ms": round(self.p99_ms, 1),
            },
            "burn_rates": [b.to_dict() for b in self.burn_rates],
            "window_days": self.window_days,
        }


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = max(0, int(len(s) * pct / 100) - 1)
    return s[idx]


class SLOTracker:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._slos: dict[str, SLODefinition] = {}
        self._events: list[SLOEvent] = []
        self._alert_callbacks: list[Any] = []
        SLO_FILE.parent.mkdir(parents=True, exist_ok=True)

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def define_slo(self, slo: SLODefinition):
        self._slos[slo.name] = slo
        log.info(
            f"SLO defined: {slo.name} target={slo.target_pct}% window={slo.window_days}d"
        )

    def on_alert(self, callback):
        self._alert_callbacks.append(callback)

    def record(
        self,
        slo_name: str,
        success: bool,
        latency_ms: float = 0.0,
        context: dict | None = None,
    ) -> SLOEvent:
        event = SLOEvent(
            slo_name=slo_name,
            success=success,
            latency_ms=latency_ms,
            context=context or {},
        )
        self._events.append(event)

        # Keep only events within max window (60 days)
        cutoff = time.time() - 60 * 86400
        if len(self._events) > 100000:
            self._events = [e for e in self._events if e.ts >= cutoff]

        # Async persist + redis
        self._persist(event)
        if self.redis:
            asyncio.create_task(self._push_redis(event))

        return event

    def _persist(self, event: SLOEvent):
        with open(SLO_FILE, "a") as f:
            f.write(json.dumps(event.to_dict()) + "\n")

    async def _push_redis(self, event: SLOEvent):
        day = time.strftime("%Y-%m-%d", time.localtime(event.ts))
        key = f"{REDIS_PREFIX}{event.slo_name}:{day}"
        if event.success:
            await self.redis.hincrby(key, "ok", 1)
        else:
            await self.redis.hincrby(key, "fail", 1)
        await self.redis.expire(key, 86400 * 65)

    def _events_in_window(self, slo_name: str, days: float) -> list[SLOEvent]:
        cutoff = time.time() - days * 86400
        return [e for e in self._events if e.slo_name == slo_name and e.ts >= cutoff]

    def _burn_rate(self, slo: SLODefinition, window_h: float) -> BurnRate:
        events = self._events_in_window(slo.name, window_h / 24)
        if not events:
            return BurnRate(window_h=window_h, rate=0.0, is_burning=False)
        failures = sum(1 for e in events if not e.success)
        error_rate = failures / len(events)
        allowed_rate = slo.error_budget_pct / 100.0
        rate = error_rate / max(allowed_rate, 1e-9)
        return BurnRate(window_h=window_h, rate=rate, is_burning=rate > 1.0)

    def report(self, slo_name: str) -> SLOReport | None:
        slo = self._slos.get(slo_name)
        if not slo:
            return None

        events = self._events_in_window(slo_name, slo.window_days)
        total = len(events)
        failures = sum(1 for e in events if not e.success)
        actual_pct = (1 - failures / max(total, 1)) * 100

        latencies = [e.latency_ms for e in events if e.latency_ms > 0]
        p50 = _percentile(latencies, 50)
        p95 = _percentile(latencies, 95)
        p99 = _percentile(latencies, 99)

        budget_total = slo.error_budget_pct / 100.0
        budget_used = (failures / max(total, 1)) / max(budget_total, 1e-9) * 100

        burn_rates = [self._burn_rate(slo, w) for w in [1, 6, 24, 72, 168]]

        return SLOReport(
            slo_name=slo_name,
            target_pct=slo.target_pct,
            actual_pct=actual_pct,
            compliant=actual_pct >= slo.target_pct,
            error_budget_used_pct=min(budget_used, 100.0),
            total_requests=total,
            failures=failures,
            p50_ms=p50,
            p95_ms=p95,
            p99_ms=p99,
            burn_rates=burn_rates,
            window_days=slo.window_days,
        )

    def check_alerts(self):
        """Check all SLOs and fire alerts for burning budgets."""
        for slo_name in self._slos:
            report = self.report(slo_name)
            if not report:
                continue
            for br in report.burn_rates:
                if br.is_burning and br.rate > 2.0:
                    for cb in self._alert_callbacks:
                        try:
                            cb(slo_name, report, br)
                        except Exception:
                            pass

    def all_reports(self) -> list[dict]:
        return [self.report(n).to_dict() for n in self._slos if self.report(n)]

    def stats(self) -> dict:
        return {
            "slos": len(self._slos),
            "events": len(self._events),
            "compliant": sum(
                1 for n in self._slos if (r := self.report(n)) and r.compliant
            ),
        }


def build_jarvis_slos() -> SLOTracker:
    tracker = SLOTracker()
    tracker.define_slo(
        SLODefinition("api_availability", target_pct=99.9, window_days=30)
    )
    tracker.define_slo(
        SLODefinition(
            "inference_latency",
            target_pct=95.0,
            window_days=7,
            latency_p99_ms=5000,
            description="95% of inferences < 5s",
        )
    )
    tracker.define_slo(SLODefinition("gpu_health", target_pct=99.5, window_days=30))
    return tracker


async def main():
    import sys
    import random

    tracker = build_jarvis_slos()
    await tracker.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        # Simulate 1000 requests with 99.5% success rate
        random.seed(42)
        for i in range(1000):
            ok = random.random() > 0.005
            lat = random.gauss(500, 200)
            tracker.record("api_availability", ok, max(50, lat))
            tracker.record("inference_latency", ok, max(50, lat))

        print("SLO Reports:")
        for report_dict in tracker.all_reports():
            status = "✅" if report_dict["compliant"] else "❌"
            print(f"\n  {status} {report_dict['slo_name']}")
            print(
                f"     Target: {report_dict['target_pct']}%  Actual: {report_dict['actual_pct']:.3f}%"
            )
            print(f"     Budget used: {report_dict['error_budget_used_pct']:.1f}%")
            print(
                f"     Latency p50/p95/p99: {report_dict['latency']['p50_ms']:.0f}/{report_dict['latency']['p95_ms']:.0f}/{report_dict['latency']['p99_ms']:.0f}ms"
            )
            for br in report_dict["burn_rates"][:3]:
                flag = "🔥" if br["is_burning"] else "  "
                print(
                    f"     {flag} {br['window_h']:>4}h burn rate: {br['burn_rate']:.2f}x"
                )

        print(f"\nStats: {json.dumps(tracker.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(tracker.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
