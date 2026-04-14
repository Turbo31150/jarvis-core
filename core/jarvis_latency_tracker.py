#!/usr/bin/env python3
"""
jarvis_latency_tracker — Fine-grained latency tracking with percentiles and anomaly detection
Tracks TTFB, total latency, queue time per model/endpoint with sliding windows
"""

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.latency_tracker")

REDIS_PREFIX = "jarvis:latency:"
LATENCY_FILE = Path("/home/turbo/IA/Core/jarvis/data/latency_log.jsonl")


@dataclass
class LatencySample:
    label: str
    phase: str  # ttfb | total | queue | processing
    value_ms: float
    ts: float = field(default_factory=time.time)
    tags: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "phase": self.phase,
            "value_ms": round(self.value_ms, 2),
            "ts": self.ts,
            "tags": self.tags,
        }


@dataclass
class LatencyStats:
    label: str
    phase: str
    n: int
    p50: float
    p75: float
    p90: float
    p95: float
    p99: float
    mean: float
    std: float
    min_val: float
    max_val: float
    anomalies: int

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "phase": self.phase,
            "n": self.n,
            "p50": round(self.p50, 1),
            "p75": round(self.p75, 1),
            "p90": round(self.p90, 1),
            "p95": round(self.p95, 1),
            "p99": round(self.p99, 1),
            "mean": round(self.mean, 1),
            "std": round(self.std, 1),
            "min": round(self.min_val, 1),
            "max": round(self.max_val, 1),
            "anomalies": self.anomalies,
        }


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = max(0, min(int(len(s) * pct / 100), len(s) - 1))
    return s[idx]


def _stats(
    values: list[float], label: str, phase: str, anomaly_threshold: float
) -> LatencyStats:
    n = len(values)
    if n == 0:
        return LatencyStats(label, phase, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    mean = sum(values) / n
    std = math.sqrt(sum((x - mean) ** 2 for x in values) / max(n - 1, 1))
    anomalies = sum(1 for v in values if v > mean + anomaly_threshold * std)
    return LatencyStats(
        label=label,
        phase=phase,
        n=n,
        p50=_percentile(values, 50),
        p75=_percentile(values, 75),
        p90=_percentile(values, 90),
        p95=_percentile(values, 95),
        p99=_percentile(values, 99),
        mean=mean,
        std=std,
        min_val=min(values),
        max_val=max(values),
        anomalies=anomalies,
    )


class LatencyTimer:
    """Context manager for measuring phases."""

    def __init__(
        self,
        tracker: "LatencyTracker",
        label: str,
        phase: str,
        tags: dict | None = None,
    ):
        self._tracker = tracker
        self._label = label
        self._phase = phase
        self._tags = tags or {}
        self._t0 = 0.0

    def __enter__(self):
        self._t0 = time.time()
        return self

    def __exit__(self, *_):
        elapsed = (time.time() - self._t0) * 1000
        self._tracker.record(self._label, self._phase, elapsed, self._tags)

    async def __aenter__(self):
        self._t0 = time.time()
        return self

    async def __aexit__(self, *_):
        elapsed = (time.time() - self._t0) * 1000
        self._tracker.record(self._label, self._phase, elapsed, self._tags)


class LatencyTracker:
    def __init__(
        self,
        window_s: float = 300.0,
        max_samples: int = 10_000,
        anomaly_sigma: float = 3.0,
    ):
        self.redis: aioredis.Redis | None = None
        self._window_s = window_s
        self._max_samples = max_samples
        self._anomaly_sigma = anomaly_sigma
        self._samples: list[LatencySample] = []
        self._alert_callbacks: list[Any] = []
        self._stats: dict[str, int] = {
            "recorded": 0,
            "anomalies": 0,
            "alerts_fired": 0,
        }
        LATENCY_FILE.parent.mkdir(parents=True, exist_ok=True)

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def on_alert(self, callback):
        self._alert_callbacks.append(callback)

    def record(
        self,
        label: str,
        phase: str,
        value_ms: float,
        tags: dict[str, str] | None = None,
    ):
        sample = LatencySample(
            label=label, phase=phase, value_ms=value_ms, tags=tags or {}
        )
        self._samples.append(sample)
        self._stats["recorded"] += 1

        # Prune old samples
        if len(self._samples) > self._max_samples:
            cutoff = time.time() - self._window_s
            self._samples = [s for s in self._samples if s.ts >= cutoff]

        # Check for anomaly
        values = self._values_for(label, phase)
        if len(values) >= 10:
            mean = sum(values) / len(values)
            std = math.sqrt(
                sum((x - mean) ** 2 for x in values) / max(len(values) - 1, 1)
            )
            if std > 0 and value_ms > mean + self._anomaly_sigma * std:
                self._stats["anomalies"] += 1
                self._fire_alert(label, phase, value_ms, mean, std)

        if self.redis:
            asyncio.create_task(
                self.redis.zadd(
                    f"{REDIS_PREFIX}{label}:{phase}",
                    {json.dumps({"v": round(value_ms, 2), "ts": sample.ts}): sample.ts},
                )
            )

    def _fire_alert(
        self, label: str, phase: str, value: float, mean: float, std: float
    ):
        self._stats["alerts_fired"] += 1
        for cb in self._alert_callbacks:
            try:
                cb(label, phase, value, mean, std)
            except Exception:
                pass

    def timer(
        self, label: str, phase: str = "total", tags: dict | None = None
    ) -> LatencyTimer:
        return LatencyTimer(self, label, phase, tags)

    def _values_for(
        self, label: str, phase: str, since_s: float | None = None
    ) -> list[float]:
        cutoff = time.time() - (since_s or self._window_s)
        return [
            s.value_ms
            for s in self._samples
            if s.label == label and s.phase == phase and s.ts >= cutoff
        ]

    def get_stats(
        self, label: str, phase: str = "total", window_s: float | None = None
    ) -> LatencyStats:
        values = self._values_for(label, phase, window_s)
        return _stats(values, label, phase, self._anomaly_sigma)

    def all_labels(self) -> list[str]:
        return list({s.label for s in self._samples})

    def summary(self, window_s: float | None = None) -> list[dict]:
        result = []
        seen = set()
        for s in self._samples:
            key = (s.label, s.phase)
            if key not in seen:
                seen.add(key)
                st = self.get_stats(s.label, s.phase, window_s)
                result.append(st.to_dict())
        return result

    def stats(self) -> dict:
        return {
            **self._stats,
            "samples": len(self._samples),
            "labels": len(self.all_labels()),
            "window_s": self._window_s,
        }


_global_tracker: LatencyTracker | None = None


def get_tracker() -> LatencyTracker:
    global _global_tracker
    if _global_tracker is None:
        _global_tracker = LatencyTracker()
    return _global_tracker


async def main():
    import sys
    import random

    tracker = LatencyTracker(window_s=60)
    await tracker.connect_redis()

    def on_anomaly(label, phase, value, mean, std):
        print(
            f"  ⚠️  ANOMALY {label}/{phase}: {value:.0f}ms (mean={mean:.0f} σ={std:.0f})"
        )

    tracker.on_alert(on_anomaly)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        random.seed(42)
        # Simulate normal traffic
        for _ in range(200):
            tracker.record("m1/qwen9b", "total", random.gauss(450, 60))
            tracker.record("m1/qwen9b", "ttfb", random.gauss(150, 30))
            tracker.record("m2/deepseek", "total", random.gauss(1200, 150))

        # Inject some anomalies
        for _ in range(5):
            tracker.record("m1/qwen9b", "total", random.gauss(3000, 200))

        print("Latency summary:")
        for s in tracker.summary():
            print(
                f"  {s['label']}/{s['phase']:<12} n={s['n']:<5} p50={s['p50']:.0f}ms p95={s['p95']:.0f}ms p99={s['p99']:.0f}ms anomalies={s['anomalies']}"
            )

        # Context manager usage
        with tracker.timer("test_op", "total"):
            await asyncio.sleep(0.01)

        print(f"\nStats: {json.dumps(tracker.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(tracker.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
