#!/usr/bin/env python3
"""
jarvis_metrics_exporter — Prometheus-compatible metrics export for JARVIS cluster
Collects system metrics, formats as Prometheus text, serves via HTTP endpoint
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from aiohttp import web
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.metrics_exporter")

REDIS_PREFIX = "jarvis:metrics:"
DEFAULT_PORT = 9100


class MetricType(str, Enum):
    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"
    SUMMARY = "summary"


@dataclass
class MetricLabel:
    name: str
    value: str


@dataclass
class MetricSample:
    name: str
    value: float
    labels: list[MetricLabel] = field(default_factory=list)
    ts: float = field(default_factory=time.time)

    def label_str(self) -> str:
        if not self.labels:
            return ""
        parts = [f'{l.name}="{l.value}"' for l in self.labels]
        return "{" + ",".join(parts) + "}"

    def to_prometheus_line(self) -> str:
        return f"{self.name}{self.label_str()} {self.value}"


@dataclass
class MetricFamily:
    name: str
    help_text: str
    metric_type: MetricType
    samples: list[MetricSample] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)

    def to_prometheus(self) -> str:
        lines = [
            f"# HELP {self.name} {self.help_text}",
            f"# TYPE {self.name} {self.metric_type.value}",
        ]
        for sample in self.samples:
            lines.append(sample.to_prometheus_line())
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.metric_type.value,
            "samples": len(self.samples),
            "updated_at": self.updated_at,
        }


class MetricsRegistry:
    def __init__(self):
        self._families: dict[str, MetricFamily] = {}
        self._collectors: list[Any] = []  # callable() → list[MetricFamily]

    def register(self, family: MetricFamily):
        self._families[family.name] = family

    def add_collector(self, fn):
        """fn() must return list[MetricFamily]."""
        self._collectors.append(fn)

    def gauge(
        self, name: str, value: float, labels: dict | None = None, help_text: str = ""
    ):
        labels_list = [MetricLabel(k, str(v)) for k, v in (labels or {}).items()]
        if name not in self._families:
            self._families[name] = MetricFamily(
                name, help_text or name, MetricType.GAUGE
            )
        fam = self._families[name]
        # Replace existing sample with same labels
        label_key = str(sorted((labels or {}).items()))
        fam.samples = [
            s
            for s in fam.samples
            if str(sorted([(l.name, l.value) for l in s.labels])) != label_key
        ]
        fam.samples.append(MetricSample(name, value, labels_list))
        fam.updated_at = time.time()

    def counter_inc(
        self,
        name: str,
        amount: float = 1.0,
        labels: dict | None = None,
        help_text: str = "",
    ):
        labels_list = [MetricLabel(k, str(v)) for k, v in (labels or {}).items()]
        if name not in self._families:
            self._families[name] = MetricFamily(
                name, help_text or name, MetricType.COUNTER
            )
        fam = self._families[name]
        label_key = str(sorted((labels or {}).items()))
        for sample in fam.samples:
            if str(sorted([(l.name, l.value) for l in sample.labels])) == label_key:
                sample.value += amount
                fam.updated_at = time.time()
                return
        fam.samples.append(MetricSample(name, amount, labels_list))
        fam.updated_at = time.time()

    def collect(self) -> list[MetricFamily]:
        all_families = list(self._families.values())
        for collector in self._collectors:
            try:
                extra = collector()
                all_families.extend(extra or [])
            except Exception as e:
                log.warning(f"Collector error: {e}")
        return all_families

    def render_prometheus(self) -> str:
        families = self.collect()
        blocks = [f.to_prometheus() for f in families if f.samples]
        return "\n\n".join(blocks) + "\n"

    def render_json(self) -> dict:
        return {
            "families": [f.to_dict() for f in self._families.values()],
            "timestamp": time.time(),
        }


class MetricsExporter:
    def __init__(
        self, port: int = DEFAULT_PORT, registry: MetricsRegistry | None = None
    ):
        self.redis: aioredis.Redis | None = None
        self._port = port
        self._registry = registry or MetricsRegistry()
        self._app = web.Application()
        self._runner: web.AppRunner | None = None
        self._setup_routes()
        self._setup_default_metrics()

    def _setup_routes(self):
        self._app.router.add_get("/metrics", self._handle_metrics)
        self._app.router.add_get("/metrics/json", self._handle_json)
        self._app.router.add_get("/health", self._handle_health)

    def _setup_default_metrics(self):
        # Register JARVIS default metric families
        self._registry.register(
            MetricFamily(
                "jarvis_requests_total", "Total requests processed", MetricType.COUNTER
            )
        )
        self._registry.register(
            MetricFamily(
                "jarvis_latency_seconds", "Request latency in seconds", MetricType.GAUGE
            )
        )
        self._registry.register(
            MetricFamily(
                "jarvis_gpu_temp_celsius",
                "GPU temperature in Celsius",
                MetricType.GAUGE,
            )
        )
        self._registry.register(
            MetricFamily(
                "jarvis_vram_used_bytes", "VRAM used in bytes", MetricType.GAUGE
            )
        )
        self._registry.register(
            MetricFamily(
                "jarvis_active_agents", "Number of active agents", MetricType.GAUGE
            )
        )

    async def _handle_metrics(self, request: web.Request) -> web.Response:
        text = self._registry.render_prometheus()
        return web.Response(text=text, content_type="text/plain; version=0.0.4")

    async def _handle_json(self, request: web.Request) -> web.Response:
        return web.json_response(self._registry.render_json())

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "ts": time.time()})

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    async def start(self):
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self._port)
        await site.start()
        log.info(f"Metrics exporter on http://0.0.0.0:{self._port}/metrics")

    async def stop(self):
        if self._runner:
            await self._runner.cleanup()

    @property
    def registry(self) -> MetricsRegistry:
        return self._registry

    def record_request(self, model: str, node: str, latency_s: float, success: bool):
        self._registry.counter_inc(
            "jarvis_requests_total",
            labels={"model": model, "node": node, "success": str(success).lower()},
        )
        self._registry.gauge(
            "jarvis_latency_seconds",
            latency_s,
            labels={"model": model, "node": node},
        )
        if self.redis:
            asyncio.create_task(
                self.redis.hset(
                    f"{REDIS_PREFIX}latency",
                    f"{model}:{node}",
                    str(round(latency_s, 4)),
                )
            )

    def record_gpu(self, node: str, gpu_id: int, temp_c: float, vram_used_mb: float):
        self._registry.gauge(
            "jarvis_gpu_temp_celsius",
            temp_c,
            labels={"node": node, "gpu": str(gpu_id)},
        )
        self._registry.gauge(
            "jarvis_vram_used_bytes",
            vram_used_mb * 1024 * 1024,
            labels={"node": node, "gpu": str(gpu_id)},
        )

    def record_agents(self, count: int, node: str = "all"):
        self._registry.gauge("jarvis_active_agents", count, labels={"node": node})

    def stats(self) -> dict:
        families = self._registry._families
        return {
            "port": self._port,
            "metric_families": len(families),
            "total_samples": sum(len(f.samples) for f in families.values()),
        }


def build_jarvis_metrics_exporter(port: int = DEFAULT_PORT) -> MetricsExporter:
    return MetricsExporter(port=port)


async def main():
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "serve":
        exporter = build_jarvis_metrics_exporter(port=9100)
        await exporter.connect_redis()
        await exporter.start()
        print("Metrics at http://localhost:9100/metrics")
        try:
            await asyncio.sleep(3600)
        except KeyboardInterrupt:
            pass
        await exporter.stop()

    elif cmd == "demo":
        exporter = build_jarvis_metrics_exporter(port=9100)
        await exporter.connect_redis()

        # Record some metrics
        exporter.record_request("qwen3.5-9b", "m1", 0.42, True)
        exporter.record_request("qwen3.5-9b", "m1", 0.38, True)
        exporter.record_request("deepseek-r1", "m2", 2.1, True)
        exporter.record_request("gemma3:4b", "ol1", 0.15, False)
        exporter.record_gpu("m1", 0, 65.0, 2404.0)
        exporter.record_gpu("m1", 1, 58.0, 1224.0)
        exporter.record_gpu("m2", 0, 71.0, 5120.0)
        exporter.record_agents(6)

        print("Prometheus output:")
        print(exporter.registry.render_prometheus()[:800])
        print(f"\nStats: {json.dumps(exporter.stats(), indent=2)}")

    elif cmd == "stats":
        exporter = build_jarvis_metrics_exporter()
        print(json.dumps(exporter.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
