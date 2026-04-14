#!/usr/bin/env python3
"""
jarvis_metric_store — Time-series metric storage with aggregation and alerting
Counters, gauges, histograms with sliding windows and Redis export
"""

import asyncio
import json
import logging
import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.metric_store")

REDIS_PREFIX = "jarvis:metrics:"


class MetricKind(str, Enum):
    COUNTER = "counter"  # monotonically increasing
    GAUGE = "gauge"  # current value (up/down)
    HISTOGRAM = "histogram"  # distribution of values
    RATE = "rate"  # events per second (derived)


@dataclass
class MetricPoint:
    ts: float
    value: float
    labels: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"ts": self.ts, "value": self.value, "labels": self.labels}


@dataclass
class MetricSeries:
    name: str
    kind: MetricKind
    description: str = ""
    unit: str = ""
    labels: dict[str, str] = field(default_factory=dict)
    # Ring buffer of recent points
    _points: deque = field(default_factory=lambda: deque(maxlen=3600))
    # Cumulative counter value
    _counter: float = 0.0
    # Current gauge value
    _gauge: float = 0.0

    def record(self, value: float, labels: dict | None = None, ts: float | None = None):
        ts = ts or time.time()
        if self.kind == MetricKind.COUNTER:
            self._counter += value
            self._points.append(MetricPoint(ts, self._counter, labels or {}))
        elif self.kind == MetricKind.GAUGE:
            self._gauge = value
            self._points.append(MetricPoint(ts, value, labels or {}))
        else:
            self._points.append(MetricPoint(ts, value, labels or {}))

    def inc(self, amount: float = 1.0):
        self.record(amount)

    def set(self, value: float):
        if self.kind == MetricKind.GAUGE:
            self.record(value)

    def current(self) -> float:
        if self.kind == MetricKind.COUNTER:
            return self._counter
        if self.kind == MetricKind.GAUGE:
            return self._gauge
        pts = list(self._points)
        return pts[-1].value if pts else 0.0

    def window(self, seconds: float) -> list[MetricPoint]:
        cutoff = time.time() - seconds
        return [p for p in self._points if p.ts >= cutoff]

    def rate(self, window_s: float = 60.0) -> float:
        pts = self.window(window_s)
        if len(pts) < 2:
            return 0.0
        if self.kind == MetricKind.COUNTER:
            return (pts[-1].value - pts[0].value) / max(window_s, 1.0)
        return len(pts) / window_s

    def aggregate(self, window_s: float = 60.0) -> dict:
        pts = [p.value for p in self.window(window_s)]
        if not pts:
            return {
                "count": 0,
                "sum": 0.0,
                "min": 0.0,
                "max": 0.0,
                "avg": 0.0,
                "p50": 0.0,
                "p95": 0.0,
                "p99": 0.0,
            }
        s = sorted(pts)
        n = len(s)
        return {
            "count": n,
            "sum": round(sum(pts), 6),
            "min": round(s[0], 6),
            "max": round(s[-1], 6),
            "avg": round(statistics.mean(pts), 6),
            "p50": round(s[n // 2], 6),
            "p95": round(s[min(int(n * 0.95), n - 1)], 6),
            "p99": round(s[min(int(n * 0.99), n - 1)], 6),
        }

    def to_dict(self, window_s: float = 60.0) -> dict:
        agg = self.aggregate(window_s)
        return {
            "name": self.name,
            "kind": self.kind.value,
            "description": self.description,
            "unit": self.unit,
            "current": self.current(),
            "rate_1m": round(self.rate(60.0), 4),
            "agg_1m": agg,
            "points_stored": len(self._points),
        }


@dataclass
class AlertRule:
    rule_id: str
    metric_name: str
    condition: str  # "gt", "lt", "eq", "gte", "lte"
    threshold: float
    window_s: float = 60.0
    use_rate: bool = False
    severity: str = "warning"
    message: str = ""
    cooldown_s: float = 300.0
    last_fired: float = 0.0

    def evaluate(self, series: MetricSeries) -> bool:
        val = series.rate(self.window_s) if self.use_rate else series.current()
        ops = {
            "gt": val > self.threshold,
            "gte": val >= self.threshold,
            "lt": val < self.threshold,
            "lte": val <= self.threshold,
            "eq": abs(val - self.threshold) < 1e-9,
        }
        return ops.get(self.condition, False)

    def can_fire(self) -> bool:
        return (time.time() - self.last_fired) >= self.cooldown_s


class MetricStore:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._series: dict[str, MetricSeries] = {}
        self._alert_rules: dict[str, AlertRule] = {}
        self._alert_callbacks: list = []
        self._stats: dict[str, int] = {
            "records": 0,
            "alerts_fired": 0,
            "series_count": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def define(
        self,
        name: str,
        kind: MetricKind = MetricKind.GAUGE,
        description: str = "",
        unit: str = "",
    ) -> MetricSeries:
        if name not in self._series:
            self._series[name] = MetricSeries(
                name=name, kind=kind, description=description, unit=unit
            )
            self._stats["series_count"] += 1
        return self._series[name]

    def _get_or_create(
        self, name: str, kind: MetricKind = MetricKind.GAUGE
    ) -> MetricSeries:
        if name not in self._series:
            return self.define(name, kind)
        return self._series[name]

    def record(
        self,
        name: str,
        value: float,
        kind: MetricKind = MetricKind.GAUGE,
        labels: dict | None = None,
    ):
        s = self._get_or_create(name, kind)
        s.record(value, labels)
        self._stats["records"] += 1
        self._check_alerts(name, s)
        if self.redis:
            asyncio.create_task(self._redis_push(name, value))

    def inc(self, name: str, amount: float = 1.0):
        s = self._get_or_create(name, MetricKind.COUNTER)
        s.inc(amount)
        self._stats["records"] += 1

    def gauge(self, name: str, value: float, labels: dict | None = None):
        s = self._get_or_create(name, MetricKind.GAUGE)
        s.set(value)
        s.record(value, labels)
        self._stats["records"] += 1

    async def _redis_push(self, name: str, value: float):
        if not self.redis:
            return
        try:
            await self.redis.hset(
                f"{REDIS_PREFIX}latest",
                name,
                json.dumps({"value": value, "ts": time.time()}),
            )
            await self.redis.expire(f"{REDIS_PREFIX}latest", 300)
        except Exception:
            pass

    def add_alert(self, rule: AlertRule):
        self._alert_rules[rule.rule_id] = rule

    def on_alert(self, callback):
        self._alert_callbacks.append(callback)

    def _check_alerts(self, name: str, series: MetricSeries):
        for rule in self._alert_rules.values():
            if rule.metric_name != name:
                continue
            if not rule.can_fire():
                continue
            if rule.evaluate(series):
                rule.last_fired = time.time()
                self._stats["alerts_fired"] += 1
                msg = (
                    rule.message
                    or f"[{rule.severity}] {name} {rule.condition} {rule.threshold}"
                )
                log.warning(f"Metric alert: {msg}")
                for cb in self._alert_callbacks:
                    try:
                        cb(rule, series)
                    except Exception:
                        pass

    def get(self, name: str) -> MetricSeries | None:
        return self._series.get(name)

    def snapshot(self, window_s: float = 60.0) -> dict[str, dict]:
        return {name: s.to_dict(window_s) for name, s in self._series.items()}

    def stats(self) -> dict:
        return {**self._stats}


def build_jarvis_metric_store() -> MetricStore:
    store = MetricStore()

    # Define standard JARVIS metrics
    store.define(
        "inference.requests", MetricKind.COUNTER, "Total inference requests", "req"
    )
    store.define(
        "inference.latency_ms", MetricKind.HISTOGRAM, "Inference latency", "ms"
    )
    store.define(
        "inference.tokens", MetricKind.COUNTER, "Total tokens processed", "tok"
    )
    store.define("gpu.temperature", MetricKind.GAUGE, "GPU temperature", "°C")
    store.define("gpu.vram_pct", MetricKind.GAUGE, "GPU VRAM usage", "%")
    store.define("system.cpu_pct", MetricKind.GAUGE, "CPU usage", "%")
    store.define("system.ram_pct", MetricKind.GAUGE, "RAM usage", "%")
    store.define("budget.cost_usd", MetricKind.COUNTER, "Total API cost", "USD")
    store.define("errors.total", MetricKind.COUNTER, "Total errors", "err")

    # Alert rules
    store.add_alert(
        AlertRule(
            "gpu-hot",
            "gpu.temperature",
            "gt",
            80.0,
            severity="critical",
            cooldown_s=60.0,
        )
    )
    store.add_alert(
        AlertRule(
            "ram-high",
            "system.ram_pct",
            "gt",
            85.0,
            severity="warning",
            cooldown_s=120.0,
        )
    )
    store.add_alert(
        AlertRule(
            "latency-high",
            "inference.latency_ms",
            "gt",
            5000.0,
            window_s=60.0,
            severity="warning",
            cooldown_s=300.0,
        )
    )

    return store


async def main():
    import sys
    import random

    store = build_jarvis_metric_store()
    await store.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Recording metrics...")
        for i in range(20):
            store.inc("inference.requests")
            store.record(
                "inference.latency_ms", random.uniform(100, 3000), MetricKind.HISTOGRAM
            )
            store.inc("inference.tokens", random.randint(100, 2000))
            store.gauge("gpu.temperature", random.uniform(55, 75))
            store.gauge("system.cpu_pct", random.uniform(10, 60))

        print("\nSnapshot (1m window):")
        for name, data in store.snapshot().items():
            if data["points_stored"] > 0:
                print(
                    f"  {name:<30} current={data['current']:<10.2f} rate/min={data['rate_1m']:.4f}"
                )

        print(f"\nStats: {json.dumps(store.stats(), indent=2)}")

    elif cmd == "snapshot":
        print(json.dumps(store.snapshot(), indent=2))

    elif cmd == "stats":
        print(json.dumps(store.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
