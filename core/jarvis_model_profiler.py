#!/usr/bin/env python3
"""
jarvis_model_profiler — LLM model performance profiling
Latency histograms, TTFT/TPS tracking, per-model benchmarks, regression alerts
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

log = logging.getLogger("jarvis.model_profiler")

REDIS_PREFIX = "jarvis:profiler:"


class ProfileMetric(str, Enum):
    TTFT = "ttft_ms"  # Time to first token
    TPS = "tps"  # Tokens per second
    LATENCY = "latency_ms"  # Total request latency
    TOKEN_COUNT = "tokens"  # Total tokens generated
    PROMPT_TOKENS = "prompt_tokens"
    ERROR_RATE = "error_rate"
    QUEUE_TIME = "queue_ms"  # Time spent waiting in queue


@dataclass
class InferenceTrace:
    model: str
    node: str
    prompt_tokens: int
    output_tokens: int
    ttft_ms: float  # ms to first token
    total_ms: float  # total duration
    queue_ms: float = 0.0
    success: bool = True
    error: str = ""
    ts: float = field(default_factory=time.time)
    request_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def tps(self) -> float:
        dur_s = (self.total_ms - self.ttft_ms) / 1000
        return self.output_tokens / max(dur_s, 0.001)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.output_tokens

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "node": self.node,
            "prompt_tokens": self.prompt_tokens,
            "output_tokens": self.output_tokens,
            "ttft_ms": round(self.ttft_ms, 2),
            "total_ms": round(self.total_ms, 2),
            "tps": round(self.tps, 2),
            "queue_ms": round(self.queue_ms, 2),
            "success": self.success,
            "ts": self.ts,
        }


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = (p / 100) * (len(s) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(s) - 1)
    frac = idx - lo
    return s[lo] + frac * (s[hi] - s[lo])


def _stddev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((x - mean) ** 2 for x in values) / (len(values) - 1)
    return math.sqrt(variance)


@dataclass
class ModelStats:
    model: str
    node: str
    sample_count: int
    error_count: int
    # Latency
    latency_p50: float
    latency_p95: float
    latency_p99: float
    latency_mean: float
    latency_stddev: float
    # TTFT
    ttft_p50: float
    ttft_p95: float
    ttft_mean: float
    # TPS
    tps_mean: float
    tps_p10: float  # slowest decile
    tps_p90: float  # fastest decile
    # Tokens
    avg_prompt_tokens: float
    avg_output_tokens: float
    # Window
    window_start: float = 0.0
    window_end: float = 0.0

    @property
    def error_rate(self) -> float:
        return self.error_count / max(self.sample_count, 1)

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "node": self.node,
            "samples": self.sample_count,
            "error_rate": round(self.error_rate, 4),
            "latency": {
                "p50": round(self.latency_p50, 1),
                "p95": round(self.latency_p95, 1),
                "p99": round(self.latency_p99, 1),
                "mean": round(self.latency_mean, 1),
                "stddev": round(self.latency_stddev, 1),
            },
            "ttft": {
                "p50": round(self.ttft_p50, 1),
                "p95": round(self.ttft_p95, 1),
                "mean": round(self.ttft_mean, 1),
            },
            "tps": {
                "mean": round(self.tps_mean, 1),
                "p10": round(self.tps_p10, 1),
                "p90": round(self.tps_p90, 1),
            },
            "tokens": {
                "avg_prompt": round(self.avg_prompt_tokens, 1),
                "avg_output": round(self.avg_output_tokens, 1),
            },
        }


@dataclass
class RegressionAlert:
    model: str
    node: str
    metric: ProfileMetric
    baseline_value: float
    current_value: float
    delta_pct: float
    threshold_pct: float
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "node": self.node,
            "metric": self.metric.value,
            "baseline": round(self.baseline_value, 2),
            "current": round(self.current_value, 2),
            "delta_pct": round(self.delta_pct, 2),
            "threshold_pct": self.threshold_pct,
        }


class ModelProfiler:
    def __init__(
        self,
        window_size: int = 1000,  # traces per model kept in memory
        regression_threshold: float = 0.20,  # 20% degradation triggers alert
    ):
        self.redis: aioredis.Redis | None = None
        self._traces: dict[str, list[InferenceTrace]] = {}  # key: model@node
        self._baselines: dict[str, dict[str, float]] = {}  # key: model@node
        self._window_size = window_size
        self._regression_threshold = regression_threshold
        self._alerts: list[RegressionAlert] = []
        self._stats: dict[str, int] = {
            "recorded": 0,
            "errors": 0,
            "alerts": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def _key(self, model: str, node: str) -> str:
        return f"{model}@{node}"

    def record(self, trace: InferenceTrace):
        key = self._key(trace.model, trace.node)
        if key not in self._traces:
            self._traces[key] = []
        bucket = self._traces[key]
        bucket.append(trace)
        if len(bucket) > self._window_size:
            bucket.pop(0)

        self._stats["recorded"] += 1
        if not trace.success:
            self._stats["errors"] += 1

        self._check_regression(key, trace)

        if self.redis:
            asyncio.create_task(self._persist(trace))

    def record_many(self, traces: list[InferenceTrace]):
        for t in traces:
            self.record(t)

    def _compute_stats(self, key: str) -> ModelStats | None:
        bucket = self._traces.get(key, [])
        if not bucket:
            return None

        model, node = key.split("@", 1) if "@" in key else (key, "unknown")
        successful = [t for t in bucket if t.success]
        if not successful:
            successful = bucket  # include errors for partial stats

        latencies = [t.total_ms for t in successful]
        ttfts = [t.ttft_ms for t in successful]
        tps_vals = [t.tps for t in successful if t.tps > 0]

        return ModelStats(
            model=model,
            node=node,
            sample_count=len(bucket),
            error_count=sum(1 for t in bucket if not t.success),
            latency_p50=_percentile(latencies, 50),
            latency_p95=_percentile(latencies, 95),
            latency_p99=_percentile(latencies, 99),
            latency_mean=sum(latencies) / max(len(latencies), 1),
            latency_stddev=_stddev(latencies),
            ttft_p50=_percentile(ttfts, 50),
            ttft_p95=_percentile(ttfts, 95),
            ttft_mean=sum(ttfts) / max(len(ttfts), 1),
            tps_mean=sum(tps_vals) / max(len(tps_vals), 1),
            tps_p10=_percentile(tps_vals, 10),
            tps_p90=_percentile(tps_vals, 90),
            avg_prompt_tokens=sum(t.prompt_tokens for t in bucket) / len(bucket),
            avg_output_tokens=sum(t.output_tokens for t in bucket) / len(bucket),
            window_start=bucket[0].ts,
            window_end=bucket[-1].ts,
        )

    def get_stats(self, model: str, node: str) -> ModelStats | None:
        return self._compute_stats(self._key(model, node))

    def all_stats(self) -> list[ModelStats]:
        results = []
        for key in self._traces:
            s = self._compute_stats(key)
            if s:
                results.append(s)
        return sorted(results, key=lambda s: s.latency_p50)

    def set_baseline(
        self, model: str, node: str, metrics: dict[str, float] | None = None
    ):
        """Set regression baseline. If metrics=None, use current stats."""
        key = self._key(model, node)
        if metrics is None:
            stats = self._compute_stats(key)
            if not stats:
                return
            metrics = {
                ProfileMetric.LATENCY: stats.latency_p95,
                ProfileMetric.TTFT: stats.ttft_p95,
                ProfileMetric.TPS: stats.tps_mean,
                ProfileMetric.ERROR_RATE: stats.error_rate,
            }
        self._baselines[key] = {str(k): v for k, v in metrics.items()}
        log.info(f"Baseline set for {key}: {metrics}")

    def _check_regression(self, key: str, trace: InferenceTrace):
        baseline = self._baselines.get(key)
        if not baseline:
            return

        checks = [
            (ProfileMetric.LATENCY, trace.total_ms, True),  # higher = worse
            (ProfileMetric.TTFT, trace.ttft_ms, True),
            (ProfileMetric.TPS, trace.tps, False),  # lower = worse
        ]

        for metric, current_val, higher_is_worse in checks:
            base = baseline.get(str(metric))
            if not base or base == 0:
                continue

            delta_pct = (current_val - base) / base * 100
            if higher_is_worse:
                triggered = delta_pct > self._regression_threshold * 100
            else:
                triggered = delta_pct < -self._regression_threshold * 100

            if triggered:
                alert = RegressionAlert(
                    model=trace.model,
                    node=trace.node,
                    metric=metric,
                    baseline_value=base,
                    current_value=current_val,
                    delta_pct=delta_pct,
                    threshold_pct=self._regression_threshold * 100,
                )
                self._alerts.append(alert)
                self._stats["alerts"] += 1
                log.warning(
                    f"Regression [{trace.model}@{trace.node}] "
                    f"{metric.value}: {base:.1f} → {current_val:.1f} "
                    f"({delta_pct:+.1f}%)"
                )

    def recent_alerts(self, limit: int = 20) -> list[dict]:
        return [a.to_dict() for a in self._alerts[-limit:]]

    def clear_alerts(self):
        self._alerts.clear()

    def top_slowest(self, n: int = 5) -> list[ModelStats]:
        return sorted(self.all_stats(), key=lambda s: -s.latency_p95)[:n]

    def top_fastest(self, n: int = 5) -> list[ModelStats]:
        return sorted(self.all_stats(), key=lambda s: s.latency_p50)[:n]

    def compare(self, model_a: str, node_a: str, model_b: str, node_b: str) -> dict:
        a = self.get_stats(model_a, node_a)
        b = self.get_stats(model_b, node_b)
        if not a or not b:
            return {"error": "insufficient data"}

        def pct_diff(va: float, vb: float) -> float:
            return (vb - va) / max(va, 0.001) * 100

        return {
            "model_a": f"{model_a}@{node_a}",
            "model_b": f"{model_b}@{node_b}",
            "latency_p95_diff_pct": round(pct_diff(a.latency_p95, b.latency_p95), 1),
            "ttft_diff_pct": round(pct_diff(a.ttft_mean, b.ttft_mean), 1),
            "tps_diff_pct": round(pct_diff(a.tps_mean, b.tps_mean), 1),
            "a": a.to_dict(),
            "b": b.to_dict(),
        }

    async def _persist(self, trace: InferenceTrace):
        if not self.redis:
            return
        try:
            key = f"{REDIS_PREFIX}{trace.model}:{trace.node}:recent"
            await self.redis.lpush(key, json.dumps(trace.to_dict()))
            await self.redis.ltrim(key, 0, 99)
            await self.redis.expire(key, 86400)
        except Exception:
            pass

    def stats(self) -> dict:
        return {
            **self._stats,
            "models_tracked": len(self._traces),
            "baselines_set": len(self._baselines),
        }


def build_jarvis_model_profiler() -> ModelProfiler:
    return ModelProfiler(window_size=500, regression_threshold=0.25)


async def main():
    import sys
    import random

    profiler = build_jarvis_model_profiler()
    await profiler.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Model profiler demo...\n")
        rng = random.Random(42)

        models = [
            ("qwen3.5-9b", "m1"),
            ("deepseek-r1", "m2"),
            ("qwen3.5-35b", "m1"),
        ]

        # Generate synthetic traces
        for _ in range(50):
            model, node = rng.choice(models)
            ttft = rng.gauss(120, 30) if model == "qwen3.5-9b" else rng.gauss(200, 50)
            total = ttft + rng.gauss(800, 200)
            trace = InferenceTrace(
                model=model,
                node=node,
                prompt_tokens=rng.randint(50, 500),
                output_tokens=rng.randint(100, 800),
                ttft_ms=max(10, ttft),
                total_ms=max(ttft + 50, total),
                success=rng.random() > 0.05,
            )
            profiler.record(trace)

        print("  All model stats:")
        for stats in profiler.all_stats():
            print(
                f"  {stats.model:<20} @{stats.node:<4} "
                f"p95={stats.latency_p95:.0f}ms "
                f"ttft={stats.ttft_mean:.0f}ms "
                f"tps={stats.tps_mean:.1f} "
                f"err={stats.error_rate:.1%}"
            )

        # Set baseline and inject regression
        profiler.set_baseline("qwen3.5-9b", "m1")
        bad_trace = InferenceTrace(
            model="qwen3.5-9b",
            node="m1",
            prompt_tokens=100,
            output_tokens=200,
            ttft_ms=800,
            total_ms=3000,
            success=True,
        )
        profiler.record(bad_trace)

        alerts = profiler.recent_alerts(5)
        print(f"\n  Alerts ({len(alerts)}):")
        for a in alerts:
            print(
                f"    {a['metric']}: {a['baseline']:.0f} → {a['current']:.0f} ({a['delta_pct']:+.0f}%)"
            )

        # Compare two models
        cmp = profiler.compare("qwen3.5-9b", "m1", "deepseek-r1", "m2")
        print(f"\n  Comparison: latency_diff={cmp.get('latency_p95_diff_pct', 'N/A')}%")

        print(f"\nStats: {json.dumps(profiler.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(profiler.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
