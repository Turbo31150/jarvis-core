#!/usr/bin/env python3
"""
jarvis_gradient_tracker — Trend detection and gradient analysis for time-series metrics
Detects rising/falling trends, acceleration, inflection points in metric streams
"""

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.gradient_tracker")

REDIS_PREFIX = "jarvis:gradient:"


class TrendDirection(str, Enum):
    RISING = "rising"
    FALLING = "falling"
    STABLE = "stable"
    VOLATILE = "volatile"


class TrendSeverity(str, Enum):
    NORMAL = "normal"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class DataPoint:
    ts: float
    value: float
    label: str = ""

    def to_dict(self) -> dict:
        return {"ts": self.ts, "value": round(self.value, 4), "label": self.label}


@dataclass
class GradientResult:
    metric: str
    direction: TrendDirection
    severity: TrendSeverity
    gradient: float  # units/second
    acceleration: float  # gradient/second (second derivative)
    r_squared: float  # linear fit quality
    window_s: float
    n_points: int
    predicted_value: float  # extrapolated value after window_s
    alert_threshold: float
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "metric": self.metric,
            "direction": self.direction.value,
            "severity": self.severity.value,
            "gradient_per_s": round(self.gradient, 4),
            "acceleration": round(self.acceleration, 6),
            "r_squared": round(self.r_squared, 4),
            "window_s": self.window_s,
            "n_points": self.n_points,
            "predicted_value": round(self.predicted_value, 4),
        }


def _linear_regression(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """Returns (slope, intercept, r_squared)."""
    n = len(xs)
    if n < 2:
        return 0.0, ys[0] if ys else 0.0, 0.0

    xm = sum(xs) / n
    ym = sum(ys) / n
    num = sum((x - xm) * (y - ym) for x, y in zip(xs, ys))
    den = sum((x - xm) ** 2 for x in xs)

    if abs(den) < 1e-12:
        return 0.0, ym, 0.0

    slope = num / den
    intercept = ym - slope * xm

    # R²
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    ss_tot = sum((y - ym) ** 2 for y in ys)
    r2 = 1 - ss_res / max(ss_tot, 1e-12)
    return slope, intercept, max(0.0, min(1.0, r2))


@dataclass
class MetricConfig:
    name: str
    warning_gradient: float = 0.0  # alert if gradient > this (per second)
    critical_gradient: float = 0.0  # critical if gradient > this
    stable_threshold: float = 0.01  # below this gradient = stable
    volatile_cv: float = 0.3  # coefficient of variation above = volatile
    window_s: float = 60.0
    max_points: int = 1000


class GradientTracker:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._series: dict[str, list[DataPoint]] = {}
        self._configs: dict[str, MetricConfig] = {}
        self._alert_callbacks: list[Any] = []
        self._stats: dict[str, int] = {
            "points_recorded": 0,
            "analyses": 0,
            "alerts": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def configure(self, config: MetricConfig):
        self._configs[config.name] = config

    def on_alert(self, callback):
        self._alert_callbacks.append(callback)

    def push(self, metric: str, value: float, ts: float | None = None, label: str = ""):
        point = DataPoint(ts=ts or time.time(), value=value, label=label)
        series = self._series.setdefault(metric, [])
        series.append(point)
        self._stats["points_recorded"] += 1

        cfg = self._configs.get(metric, MetricConfig(name=metric))

        # Prune old points
        cutoff = time.time() - cfg.window_s * 2
        if len(series) > cfg.max_points or (series and series[0].ts < cutoff):
            self._series[metric] = [p for p in series if p.ts >= cutoff]

        if self.redis:
            asyncio.create_task(
                self.redis.zadd(
                    f"{REDIS_PREFIX}{metric}",
                    {json.dumps({"v": round(value, 4), "ts": point.ts}): point.ts},
                )
            )

    def analyze(
        self, metric: str, window_s: float | None = None
    ) -> GradientResult | None:
        series = self._series.get(metric, [])
        cfg = self._configs.get(metric, MetricConfig(name=metric))
        w = window_s or cfg.window_s
        cutoff = time.time() - w

        window_points = [p for p in series if p.ts >= cutoff]
        if len(window_points) < 3:
            return None

        self._stats["analyses"] += 1

        # Normalize timestamps to start from 0
        t0 = window_points[0].ts
        xs = [p.ts - t0 for p in window_points]
        ys = [p.value for p in window_points]

        slope, intercept, r2 = _linear_regression(xs, ys)

        # Second derivative: run regression on first half vs second half slopes
        mid = len(window_points) // 2
        if mid >= 2:
            xs1 = [p.ts - t0 for p in window_points[:mid]]
            ys1 = [p.value for p in window_points[:mid]]
            xs2 = [p.ts - t0 for p in window_points[mid:]]
            ys2 = [p.value for p in window_points[mid:]]
            slope1, _, _ = _linear_regression(xs1, ys1)
            slope2, _, _ = _linear_regression(xs2, ys2)
            dt = (xs2[-1] - xs1[0]) / 2 if xs2 and xs1 else 1.0
            acceleration = (slope2 - slope1) / max(dt, 1.0)
        else:
            acceleration = 0.0

        # Volatility check
        mean_v = sum(ys) / len(ys)
        std_v = math.sqrt(sum((y - mean_v) ** 2 for y in ys) / max(len(ys) - 1, 1))
        cv = std_v / max(abs(mean_v), 1e-9)

        # Determine direction
        stable_thr = cfg.stable_threshold
        if cv > cfg.volatile_cv:
            direction = TrendDirection.VOLATILE
        elif abs(slope) < stable_thr:
            direction = TrendDirection.STABLE
        elif slope > 0:
            direction = TrendDirection.RISING
        else:
            direction = TrendDirection.FALLING

        # Severity
        abs_slope = abs(slope)
        if cfg.critical_gradient > 0 and abs_slope >= cfg.critical_gradient:
            severity = TrendSeverity.CRITICAL
        elif cfg.warning_gradient > 0 and abs_slope >= cfg.warning_gradient:
            severity = TrendSeverity.WARNING
        else:
            severity = TrendSeverity.NORMAL

        predicted = slope * w + (slope * xs[-1] + intercept) if xs else mean_v

        result = GradientResult(
            metric=metric,
            direction=direction,
            severity=severity,
            gradient=slope,
            acceleration=acceleration,
            r_squared=r2,
            window_s=w,
            n_points=len(window_points),
            predicted_value=predicted,
            alert_threshold=cfg.warning_gradient,
        )

        if severity != TrendSeverity.NORMAL:
            self._stats["alerts"] += 1
            for cb in self._alert_callbacks:
                try:
                    cb(result)
                except Exception:
                    pass

        return result

    def analyze_all(self) -> list[GradientResult]:
        results = []
        for metric in self._series:
            r = self.analyze(metric)
            if r:
                results.append(r)
        return results

    def last_value(self, metric: str) -> float | None:
        series = self._series.get(metric, [])
        return series[-1].value if series else None

    def stats(self) -> dict:
        return {**self._stats, "metrics": len(self._series)}


def build_jarvis_gradient_tracker() -> GradientTracker:
    tracker = GradientTracker()
    tracker.configure(
        MetricConfig(
            "gpu_temp",
            warning_gradient=0.5,
            critical_gradient=1.5,
            stable_threshold=0.05,
            window_s=120,
        )
    )
    tracker.configure(
        MetricConfig(
            "error_rate",
            warning_gradient=0.01,
            critical_gradient=0.05,
            stable_threshold=0.001,
            window_s=60,
        )
    )
    tracker.configure(
        MetricConfig(
            "latency_ms",
            warning_gradient=10.0,
            critical_gradient=50.0,
            stable_threshold=1.0,
            window_s=60,
        )
    )
    tracker.configure(
        MetricConfig(
            "vram_used_mb", warning_gradient=50.0, critical_gradient=200.0, window_s=300
        )
    )
    return tracker


async def main():
    import sys
    import random

    tracker = build_jarvis_gradient_tracker()
    await tracker.connect_redis()

    def on_alert(result: GradientResult):
        print(
            f"  🔴 ALERT {result.metric}: {result.severity.value} gradient={result.gradient:.4f}/s"
        )

    tracker.on_alert(on_alert)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        random.seed(42)
        now = time.time()

        # Simulate rising GPU temperature
        for i in range(60):
            tracker.push(
                "gpu_temp", 60 + i * 0.3 + random.gauss(0, 0.5), ts=now - 60 + i
            )

        # Simulate stable latency
        for i in range(60):
            tracker.push("latency_ms", 450 + random.gauss(0, 20), ts=now - 60 + i)

        # Simulate rising error rate
        for i in range(60):
            tracker.push(
                "error_rate", 0.01 + i * 0.001 + random.gauss(0, 0.001), ts=now - 60 + i
            )

        print("Gradient analysis:")
        for result in tracker.analyze_all():
            icon = {"normal": "✅", "warning": "⚠️", "critical": "🔴"}.get(
                result.severity.value, "?"
            )
            print(
                f"  {icon} {result.metric:<20} {result.direction.value:<10} "
                f"grad={result.gradient:+.4f}/s r²={result.r_squared:.3f} "
                f"pred={result.predicted_value:.1f}"
            )

        print(f"\nStats: {json.dumps(tracker.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(tracker.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
