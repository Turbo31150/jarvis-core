#!/usr/bin/env python3
"""
jarvis_metric_aggregator — Time-series metric collection and aggregation
Collects counters, gauges, histograms; computes p50/p95/p99; exports to Redis
"""

import asyncio
import json
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from statistics import mean

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.metric_aggregator")

REDIS_PREFIX = "jarvis:metrics:"
FLUSH_INTERVAL_S = 10.0
MAX_SAMPLES = 1000


@dataclass
class MetricPoint:
    name: str
    value: float
    labels: dict[str, str]
    ts: float = field(default_factory=time.time)


class Histogram:
    def __init__(self, max_samples: int = MAX_SAMPLES):
        self._samples: deque[float] = deque(maxlen=max_samples)

    def observe(self, value: float):
        self._samples.append(value)

    def percentile(self, p: float) -> float:
        if not self._samples:
            return 0.0
        sorted_s = sorted(self._samples)
        idx = int(len(sorted_s) * p / 100)
        return sorted_s[min(idx, len(sorted_s) - 1)]

    def summary(self) -> dict:
        if not self._samples:
            return {"count": 0}
        s = list(self._samples)
        return {
            "count": len(s),
            "min": round(min(s), 3),
            "max": round(max(s), 3),
            "mean": round(mean(s), 3),
            "p50": round(self.percentile(50), 3),
            "p95": round(self.percentile(95), 3),
            "p99": round(self.percentile(99), 3),
        }


class MetricAggregator:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._counters: dict[str, float] = defaultdict(float)
        self._gauges: dict[str, float] = {}
        self._histograms: dict[str, Histogram] = defaultdict(Histogram)
        self._flush_task: asyncio.Task | None = None

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    # ── Record methods ─────────────────────────────────────────────────────────

    def inc(self, name: str, value: float = 1.0, labels: dict | None = None):
        """Increment a counter."""
        key = self._key(name, labels)
        self._counters[key] += value

    def gauge(self, name: str, value: float, labels: dict | None = None):
        """Set a gauge to an absolute value."""
        key = self._key(name, labels)
        self._gauges[key] = value

    def observe(self, name: str, value: float, labels: dict | None = None):
        """Add a histogram observation (latency, size, etc.)."""
        key = self._key(name, labels)
        self._histograms[key].observe(value)

    def _key(self, name: str, labels: dict | None) -> str:
        if not labels:
            return name
        label_str = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        return f"{name}{{{label_str}}}"

    # ── Flush to Redis ─────────────────────────────────────────────────────────

    async def flush(self):
        if not self.redis:
            return
        ts = time.time()
        pipe = self.redis.pipeline()

        for key, val in self._counters.items():
            pipe.hset(f"{REDIS_PREFIX}counters", key, val)

        for key, val in self._gauges.items():
            pipe.hset(f"{REDIS_PREFIX}gauges", key, val)

        for key, hist in self._histograms.items():
            s = hist.summary()
            pipe.hset(f"{REDIS_PREFIX}histograms", key, json.dumps(s))

        pipe.set(f"{REDIS_PREFIX}last_flush", ts)
        await pipe.execute()
        log.debug(
            f"Flushed: {len(self._counters)} counters, {len(self._gauges)} gauges, "
            f"{len(self._histograms)} histograms"
        )

    async def start_flush_loop(self, interval_s: float = FLUSH_INTERVAL_S):
        async def loop():
            while True:
                await asyncio.sleep(interval_s)
                try:
                    await self.flush()
                except Exception as e:
                    log.error(f"Flush error: {e}")

        self._flush_task = asyncio.create_task(loop())

    def stop_flush_loop(self):
        if self._flush_task:
            self._flush_task.cancel()

    # ── Read methods ───────────────────────────────────────────────────────────

    def get_counter(self, name: str, labels: dict | None = None) -> float:
        return self._counters.get(self._key(name, labels), 0.0)

    def get_gauge(self, name: str, labels: dict | None = None) -> float | None:
        return self._gauges.get(self._key(name, labels))

    def get_histogram(self, name: str, labels: dict | None = None) -> dict:
        key = self._key(name, labels)
        return self._histograms[key].summary()

    def snapshot(self) -> dict:
        return {
            "ts": time.time(),
            "counters": dict(self._counters),
            "gauges": dict(self._gauges),
            "histograms": {k: v.summary() for k, v in self._histograms.items()},
        }

    async def load_from_redis(self) -> dict:
        if not self.redis:
            return {}
        counters = await self.redis.hgetall(f"{REDIS_PREFIX}counters")
        gauges = await self.redis.hgetall(f"{REDIS_PREFIX}gauges")
        raw_hists = await self.redis.hgetall(f"{REDIS_PREFIX}histograms")
        histograms = {}
        for k, v in raw_hists.items():
            try:
                histograms[k] = json.loads(v)
            except Exception:
                pass
        return {
            "counters": {k: float(v) for k, v in counters.items()},
            "gauges": {k: float(v) for k, v in gauges.items()},
            "histograms": histograms,
        }

    def reset(self, name: str | None = None):
        """Reset all metrics or a specific one."""
        if name is None:
            self._counters.clear()
            self._gauges.clear()
            self._histograms.clear()
        else:
            keys = [
                k
                for k in list(self._counters)
                + list(self._gauges)
                + list(self._histograms)
                if k.startswith(name)
            ]
            for k in keys:
                self._counters.pop(k, None)
                self._gauges.pop(k, None)
                self._histograms.pop(k, None)


async def main():
    import sys

    agg = MetricAggregator()
    await agg.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        import random

        # Simulate some metrics
        for i in range(50):
            agg.inc("requests_total", labels={"model": "qwen3.5-9b"})
            agg.inc(
                "tokens_total",
                random.randint(100, 1000),
                labels={"model": "qwen3.5-9b"},
            )
            agg.observe(
                "latency_ms", random.gauss(250, 80), labels={"model": "qwen3.5-9b"}
            )
            if i % 10 == 0:
                agg.inc("errors_total", labels={"model": "qwen3.5-9b"})

        agg.gauge("gpu_temp_c", 62.0, labels={"gpu": "0"})
        agg.gauge("vram_used_mb", 2400, labels={"gpu": "0"})

        await agg.flush()

        snap = agg.snapshot()
        print(f"Counters: {len(snap['counters'])}")
        for k, v in snap["counters"].items():
            print(f"  {k} = {v}")
        print("\nGauges:")
        for k, v in snap["gauges"].items():
            print(f"  {k} = {v}")
        print("\nHistograms:")
        for k, v in snap["histograms"].items():
            print(f"  {k}: p50={v.get('p50')} p95={v.get('p95')} p99={v.get('p99')}")

    elif cmd == "snapshot":
        data = await agg.load_from_redis()
        print(json.dumps(data, indent=2))

    elif cmd == "inc" and len(sys.argv) > 2:
        agg.inc(sys.argv[2], float(sys.argv[3]) if len(sys.argv) > 3 else 1.0)
        await agg.flush()
        print(f"Incremented {sys.argv[2]}")


if __name__ == "__main__":
    asyncio.run(main())
