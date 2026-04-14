#!/usr/bin/env python3
"""
jarvis_telemetry_exporter — Metrics export to Prometheus/InfluxDB/StatsD/OTLP
Collects internal counters, gauges, histograms and exports via multiple backends
"""

import asyncio
import json
import logging
import socket
import time
from dataclasses import dataclass, field
from enum import Enum

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.telemetry_exporter")

REDIS_PREFIX = "jarvis:telemetry:"


class MetricType(str, Enum):
    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"
    SUMMARY = "summary"


class ExportFormat(str, Enum):
    PROMETHEUS = "prometheus"
    INFLUX = "influx"
    STATSD = "statsd"
    JSON = "json"


@dataclass
class MetricSample:
    name: str
    value: float
    labels: dict[str, str] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    metric_type: MetricType = MetricType.GAUGE

    def label_str(self) -> str:
        if not self.labels:
            return ""
        parts = ",".join(f'{k}="{v}"' for k, v in sorted(self.labels.items()))
        return "{" + parts + "}"

    def to_prometheus(self) -> str:
        return f"{self.name}{self.label_str()} {self.value} {int(self.ts * 1000)}"

    def to_influx(self) -> str:
        tags = ",".join(f"{k}={v}" for k, v in self.labels.items())
        measurement = f"{self.name},{tags}" if tags else self.name
        return f"{measurement} value={self.value} {int(self.ts * 1e9)}"

    def to_statsd(self) -> str:
        suffix = {"counter": "c", "gauge": "g", "histogram": "ms"}.get(
            self.metric_type.value, "g"
        )
        return f"{self.name}:{self.value}|{suffix}"


@dataclass
class Histogram:
    name: str
    labels: dict[str, str]
    buckets: list[float] = field(
        default_factory=lambda: [5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000]
    )
    _values: list[float] = field(default_factory=list)

    def observe(self, value: float):
        self._values.append(value)
        if len(self._values) > 10000:
            self._values = self._values[-5000:]

    def samples(self) -> list[MetricSample]:
        if not self._values:
            return []
        ts = time.time()
        result = []
        for bucket in self.buckets:
            count = sum(1 for v in self._values if v <= bucket)
            result.append(
                MetricSample(
                    f"{self.name}_bucket",
                    count,
                    {**self.labels, "le": str(bucket)},
                    ts,
                    MetricType.COUNTER,
                )
            )
        result.append(
            MetricSample(
                f"{self.name}_bucket",
                len(self._values),
                {**self.labels, "le": "+Inf"},
                ts,
            )
        )
        result.append(
            MetricSample(f"{self.name}_count", len(self._values), self.labels, ts)
        )
        result.append(
            MetricSample(f"{self.name}_sum", sum(self._values), self.labels, ts)
        )
        return result

    def percentile(self, pct: float) -> float:
        if not self._values:
            return 0.0
        s = sorted(self._values)
        idx = max(0, int(len(s) * pct / 100) - 1)
        return s[idx]


class TelemetryExporter:
    def __init__(self, default_labels: dict[str, str] | None = None):
        self.redis: aioredis.Redis | None = None
        self._default_labels = default_labels or {}
        self._counters: dict[str, float] = {}
        self._gauges: dict[str, MetricSample] = {}
        self._histograms: dict[str, Histogram] = {}
        self._export_targets: list[dict] = []
        self._stats: dict[str, int] = {"exports": 0, "samples": 0, "errors": 0}

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def _make_key(self, name: str, labels: dict) -> str:
        lstr = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        return f"{name}|{lstr}"

    def counter(self, name: str, value: float = 1.0, labels: dict | None = None):
        key = self._make_key(name, {**self._default_labels, **(labels or {})})
        self._counters[key] = self._counters.get(key, 0.0) + value

    def gauge(self, name: str, value: float, labels: dict | None = None):
        merged = {**self._default_labels, **(labels or {})}
        key = self._make_key(name, merged)
        self._gauges[key] = MetricSample(
            name, value, merged, time.time(), MetricType.GAUGE
        )

    def histogram(
        self,
        name: str,
        value: float,
        labels: dict | None = None,
        buckets: list[float] | None = None,
    ):
        merged = {**self._default_labels, **(labels or {})}
        key = self._make_key(name, merged)
        if key not in self._histograms:
            self._histograms[key] = Histogram(
                name,
                merged,
                buckets or [5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000],
            )
        self._histograms[key].observe(value)

    def add_prometheus_target(self, push_url: str, job: str = "jarvis"):
        self._export_targets.append(
            {"format": ExportFormat.PROMETHEUS, "url": push_url, "job": job}
        )

    def add_statsd_target(self, host: str = "127.0.0.1", port: int = 8125):
        self._export_targets.append(
            {"format": ExportFormat.STATSD, "host": host, "port": port}
        )

    def add_redis_target(self):
        self._export_targets.append({"format": ExportFormat.JSON})

    def collect(self) -> list[MetricSample]:
        samples = []
        ts = time.time()
        for key, value in self._counters.items():
            name, _, label_str = key.partition("|")
            labels = dict(kv.split("=", 1) for kv in label_str.split(",") if "=" in kv)
            samples.append(MetricSample(name, value, labels, ts, MetricType.COUNTER))
        for s in self._gauges.values():
            samples.append(s)
        for h in self._histograms.values():
            samples.extend(h.samples())
        return samples

    def render_prometheus(self) -> str:
        lines = []
        for s in self.collect():
            lines.append(s.to_prometheus())
        return "\n".join(lines) + "\n"

    async def export(self) -> int:
        samples = self.collect()
        self._stats["samples"] += len(samples)
        exported = 0

        for target in self._export_targets:
            fmt = target["format"]
            try:
                if fmt == ExportFormat.PROMETHEUS and "url" in target:
                    body = "\n".join(s.to_prometheus() for s in samples)
                    async with aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=5)
                    ) as sess:
                        await sess.post(target["url"], data=body)
                    exported += 1

                elif fmt == ExportFormat.STATSD:
                    lines = [s.to_statsd() for s in samples]
                    payload = "\n".join(lines).encode()
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    sock.sendto(payload, (target["host"], target["port"]))
                    sock.close()
                    exported += 1

                elif fmt == ExportFormat.JSON and self.redis:
                    data = [s.__dict__ for s in samples[:100]]
                    await self.redis.setex(
                        f"{REDIS_PREFIX}snapshot",
                        300,
                        json.dumps({"ts": time.time(), "samples": data}),
                    )
                    exported += 1

            except Exception as e:
                log.debug(f"Export error ({fmt}): {e}")
                self._stats["errors"] += 1

        self._stats["exports"] += 1
        return exported

    async def run_loop(self, interval_s: float = 15.0):
        while True:
            await self.export()
            await asyncio.sleep(interval_s)

    def snapshot(self) -> dict:
        samples = self.collect()
        return {
            "ts": time.time(),
            "counters": len(self._counters),
            "gauges": len(self._gauges),
            "histograms": len(self._histograms),
            "total_samples": len(samples),
        }

    def stats(self) -> dict:
        return {**self._stats, **self.snapshot(), "targets": len(self._export_targets)}


def build_jarvis_telemetry() -> TelemetryExporter:
    tel = TelemetryExporter(default_labels={"service": "jarvis", "cluster": "m1"})
    tel.add_redis_target()
    return tel


async def main():
    import sys

    tel = build_jarvis_telemetry()
    await tel.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        # Simulate metrics
        for i in range(50):
            tel.counter("jarvis_requests_total", labels={"model": "qwen3.5"})
            tel.gauge("jarvis_gpu_temp_celsius", 65.0 + i * 0.1, labels={"gpu": "0"})
            tel.histogram("jarvis_latency_ms", 100 + i * 10)

        tel.gauge("jarvis_active_connections", 12.0)
        tel.counter("jarvis_errors_total", 3, labels={"type": "timeout"})

        print("Prometheus output (first 5 lines):")
        for line in tel.render_prometheus().strip().split("\n")[:5]:
            print(f"  {line}")

        exported = await tel.export()
        print(f"\nExported to {exported} targets")
        print(f"Stats: {json.dumps(tel.stats(), indent=2)}")

        # Histogram percentiles
        for key, h in tel._histograms.items():
            print(
                f"\nHistogram {h.name}: p50={h.percentile(50):.0f}ms p95={h.percentile(95):.0f}ms p99={h.percentile(99):.0f}ms"
            )

    elif cmd == "stats":
        print(json.dumps(tel.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
