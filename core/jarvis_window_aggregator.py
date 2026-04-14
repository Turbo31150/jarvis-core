#!/usr/bin/env python3
"""
jarvis_window_aggregator — Tumbling and sliding window aggregations over streams
Count, sum, min, max, avg, p50, p95, p99 over configurable time windows
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

log = logging.getLogger("jarvis.window_aggregator")

REDIS_PREFIX = "jarvis:winagg:"


class WindowKind(str, Enum):
    TUMBLING = "tumbling"  # non-overlapping fixed-size windows
    SLIDING = "sliding"  # continuous rolling window
    SESSION = "session"  # gap-based session windows


class AggFunc(str, Enum):
    COUNT = "count"
    SUM = "sum"
    MIN = "min"
    MAX = "max"
    AVG = "avg"
    P50 = "p50"
    P95 = "p95"
    P99 = "p99"
    STDDEV = "stddev"
    RATE = "rate"  # events/second
    FIRST = "first"
    LAST = "last"


@dataclass
class DataPoint:
    value: float
    ts: float = field(default_factory=time.time)
    labels: dict[str, str] = field(default_factory=dict)


@dataclass
class WindowResult:
    key: str
    window_kind: WindowKind
    window_s: float
    start_ts: float
    end_ts: float
    aggregations: dict[str, float]
    count: int
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "kind": self.window_kind.value,
            "window_s": self.window_s,
            "start_ts": round(self.start_ts, 3),
            "end_ts": round(self.end_ts, 3),
            "count": self.count,
            "aggregations": {k: round(v, 6) for k, v in self.aggregations.items()},
            "ts": self.ts,
        }


def _compute_aggs(
    values: list[float],
    funcs: list[AggFunc],
    window_s: float,
) -> dict[str, float]:
    if not values:
        return {f.value: 0.0 for f in funcs}

    result: dict[str, float] = {}
    sorted_vals = sorted(values)
    n = len(values)

    for func in funcs:
        if func == AggFunc.COUNT:
            result[func.value] = float(n)
        elif func == AggFunc.SUM:
            result[func.value] = sum(values)
        elif func == AggFunc.MIN:
            result[func.value] = min(values)
        elif func == AggFunc.MAX:
            result[func.value] = max(values)
        elif func == AggFunc.AVG:
            result[func.value] = sum(values) / n
        elif func == AggFunc.P50:
            result[func.value] = sorted_vals[n // 2]
        elif func == AggFunc.P95:
            result[func.value] = sorted_vals[min(int(n * 0.95), n - 1)]
        elif func == AggFunc.P99:
            result[func.value] = sorted_vals[min(int(n * 0.99), n - 1)]
        elif func == AggFunc.STDDEV:
            result[func.value] = statistics.stdev(values) if n >= 2 else 0.0
        elif func == AggFunc.RATE:
            result[func.value] = n / max(window_s, 1.0)
        elif func == AggFunc.FIRST:
            result[func.value] = values[0]
        elif func == AggFunc.LAST:
            result[func.value] = values[-1]

    return result


@dataclass
class WindowConfig:
    key: str
    kind: WindowKind
    window_s: float
    funcs: list[AggFunc]
    session_gap_s: float = 30.0  # for SESSION windows
    emit_empty: bool = False  # emit result even if no data


class WindowAggregator:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._configs: dict[str, WindowConfig] = {}
        # Sliding: key → deque of DataPoints
        self._buffers: dict[str, deque] = {}
        # Tumbling: key → (window_start, list[float])
        self._tumbling_state: dict[str, tuple[float, list[float]]] = {}
        # Session: key → (last_ts, list[float])
        self._session_state: dict[str, tuple[float, list[float]]] = {}
        # Emitted results
        self._results: dict[str, WindowResult] = {}
        self._callbacks: list = []
        self._stats: dict[str, int] = {
            "points_ingested": 0,
            "windows_emitted": 0,
        }
        self._auto_task: asyncio.Task | None = None

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def define(
        self,
        key: str,
        kind: WindowKind = WindowKind.SLIDING,
        window_s: float = 60.0,
        funcs: list[AggFunc] | None = None,
        session_gap_s: float = 30.0,
    ) -> WindowConfig:
        cfg = WindowConfig(
            key=key,
            kind=kind,
            window_s=window_s,
            funcs=funcs or [AggFunc.COUNT, AggFunc.AVG, AggFunc.P95],
            session_gap_s=session_gap_s,
        )
        self._configs[key] = cfg
        if kind == WindowKind.SLIDING:
            self._buffers[key] = deque()
        elif kind == WindowKind.TUMBLING:
            self._tumbling_state[key] = (time.time(), [])
        elif kind == WindowKind.SESSION:
            self._session_state[key] = (0.0, [])
        return cfg

    def push(
        self,
        key: str,
        value: float,
        ts: float | None = None,
        labels: dict | None = None,
    ):
        ts = ts or time.time()
        cfg = self._configs.get(key)
        if not cfg:
            cfg = self.define(key)

        self._stats["points_ingested"] += 1
        dp = DataPoint(value=value, ts=ts, labels=labels or {})

        if cfg.kind == WindowKind.SLIDING:
            self._buffers[key].append(dp)
            # Purge old points
            cutoff = ts - cfg.window_s
            while self._buffers[key] and self._buffers[key][0].ts < cutoff:
                self._buffers[key].popleft()

        elif cfg.kind == WindowKind.TUMBLING:
            win_start, vals = self._tumbling_state[key]
            if ts >= win_start + cfg.window_s:
                # Emit current window
                if vals or cfg.emit_empty:
                    self._emit(key, cfg, win_start, win_start + cfg.window_s, vals)
                # Start new window
                win_start = ts - (ts % cfg.window_s)
                vals = []
            vals.append(value)
            self._tumbling_state[key] = (win_start, vals)

        elif cfg.kind == WindowKind.SESSION:
            last_ts, vals = self._session_state[key]
            if last_ts > 0 and (ts - last_ts) > cfg.session_gap_s:
                # Session gap → emit
                if vals:
                    self._emit(key, cfg, last_ts - len(vals), last_ts, vals)
                vals = []
            vals.append(value)
            self._session_state[key] = (ts, vals)

    def _emit(
        self,
        key: str,
        cfg: WindowConfig,
        start: float,
        end: float,
        values: list[float],
    ) -> WindowResult:
        aggs = _compute_aggs(values, cfg.funcs, cfg.window_s)
        result = WindowResult(
            key=key,
            window_kind=cfg.kind,
            window_s=cfg.window_s,
            start_ts=start,
            end_ts=end,
            aggregations=aggs,
            count=len(values),
        )
        self._results[key] = result
        self._stats["windows_emitted"] += 1

        for cb in self._callbacks:
            try:
                cb(result)
            except Exception:
                pass

        if self.redis:
            asyncio.create_task(self._redis_emit(result))

        return result

    async def _redis_emit(self, result: WindowResult):
        if not self.redis:
            return
        try:
            await self.redis.setex(
                f"{REDIS_PREFIX}{result.key}",
                int(result.window_s * 3),
                json.dumps(result.to_dict()),
            )
        except Exception:
            pass

    def query(self, key: str) -> WindowResult | None:
        """Get current aggregation for a sliding window key."""
        cfg = self._configs.get(key)
        if not cfg or cfg.kind != WindowKind.SLIDING:
            return self._results.get(key)

        buf = self._buffers.get(key, deque())
        values = [dp.value for dp in buf]
        now = time.time()
        if not values:
            return None
        aggs = _compute_aggs(values, cfg.funcs, cfg.window_s)
        return WindowResult(
            key=key,
            window_kind=cfg.kind,
            window_s=cfg.window_s,
            start_ts=now - cfg.window_s,
            end_ts=now,
            aggregations=aggs,
            count=len(values),
        )

    def on_emit(self, callback):
        self._callbacks.append(callback)

    def snapshot(self) -> dict[str, dict]:
        result = {}
        for key in self._configs:
            r = self.query(key)
            if r:
                result[key] = r.to_dict()
        return result

    def stats(self) -> dict:
        return {
            **self._stats,
            "keys_configured": len(self._configs),
        }


def build_jarvis_window_aggregator() -> WindowAggregator:
    agg = WindowAggregator()

    agg.define(
        "inference.latency_ms",
        WindowKind.SLIDING,
        60.0,
        [AggFunc.COUNT, AggFunc.AVG, AggFunc.P95, AggFunc.P99, AggFunc.MAX],
    )
    agg.define(
        "inference.requests", WindowKind.TUMBLING, 60.0, [AggFunc.COUNT, AggFunc.RATE]
    )
    agg.define(
        "gpu.temperature",
        WindowKind.SLIDING,
        300.0,
        [AggFunc.AVG, AggFunc.MAX, AggFunc.MIN],
    )
    agg.define("errors", WindowKind.TUMBLING, 60.0, [AggFunc.COUNT, AggFunc.RATE])
    agg.define(
        "trading.signals",
        WindowKind.SESSION,
        0.0,
        [AggFunc.COUNT, AggFunc.SUM],
        session_gap_s=60.0,
    )

    return agg


async def main():
    import sys
    import random

    agg = build_jarvis_window_aggregator()
    await agg.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Window aggregator demo...")

        for i in range(50):
            agg.push("inference.latency_ms", random.uniform(100, 3000))
            agg.push("gpu.temperature", random.uniform(55, 80))
            agg.push("inference.requests", 1.0)
            if random.random() < 0.1:
                agg.push("errors", 1.0)

        print("\nCurrent window aggregations:")
        for key, data in agg.snapshot().items():
            aggs = data.get("aggregations", {})
            agg_str = " ".join(f"{k}={v:.2f}" for k, v in aggs.items())
            print(f"  {key:<30} count={data['count']} {agg_str}")

        print(f"\nStats: {json.dumps(agg.stats(), indent=2)}")

    elif cmd == "snapshot":
        print(json.dumps(agg.snapshot(), indent=2))

    elif cmd == "stats":
        print(json.dumps(agg.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
