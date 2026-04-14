#!/usr/bin/env python3
"""
jarvis_node_exporter — Prometheus-compatible metrics exporter for JARVIS cluster
Exposes GPU, RAM, LLM, Redis metrics at /metrics endpoint in text format
"""

import asyncio
import logging
import subprocess
import time

import aiohttp
import redis.asyncio as aioredis
from aiohttp import web

log = logging.getLogger("jarvis.node_exporter")

EXPORTER_PORT = 9101
NODES = {"M1": "http://127.0.0.1:1234", "M2": "http://192.168.1.26:1234"}


def _prom_line(
    name: str, value: float, labels: dict | None = None, ts: int | None = None
) -> str:
    if labels:
        label_str = ",".join(f'{k}="{v}"' for k, v in labels.items())
        metric = f"{name}{{{label_str}}} {value}"
    else:
        metric = f"{name} {value}"
    if ts:
        metric += f" {ts}"
    return metric


class NodeExporter:
    def __init__(self, port: int = EXPORTER_PORT):
        self.port = port
        self.redis: aioredis.Redis | None = None
        self._cache: str = ""
        self._cache_ts: float = 0.0
        self._cache_ttl: float = 10.0

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    async def collect_gpu(self) -> list[str]:
        lines = []
        try:
            r = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=index,name,temperature.gpu,power.draw,memory.used,memory.total,utilization.gpu",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            for line in r.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                p = [x.strip() for x in line.split(",")]
                if len(p) < 7:
                    continue
                idx, name = p[0], p[1].replace(" ", "_")
                lbl = {"gpu": idx, "name": name}
                lines += [
                    _prom_line("jarvis_gpu_temp_celsius", float(p[2]), lbl),
                    _prom_line("jarvis_gpu_power_watts", float(p[3]), lbl),
                    _prom_line("jarvis_gpu_vram_used_mb", float(p[4]), lbl),
                    _prom_line("jarvis_gpu_vram_total_mb", float(p[5]), lbl),
                    _prom_line("jarvis_gpu_utilization_pct", float(p[6]), lbl),
                ]
        except Exception as e:
            log.warning(f"GPU collect: {e}")
        return lines

    async def collect_system(self) -> list[str]:
        lines = []
        try:
            with open("/proc/meminfo") as f:
                mem = {}
                for line in f:
                    k, v = line.split(":")
                    mem[k.strip()] = int(v.strip().split()[0])
            total = mem.get("MemTotal", 0)
            avail = mem.get("MemAvailable", 0)
            lines += [
                _prom_line("jarvis_mem_total_kb", total),
                _prom_line("jarvis_mem_available_kb", avail),
                _prom_line("jarvis_mem_used_kb", total - avail),
                _prom_line(
                    "jarvis_mem_used_pct",
                    round((total - avail) / max(total, 1) * 100, 1),
                ),
            ]
        except Exception:
            pass
        try:
            with open("/proc/loadavg") as f:
                parts = f.read().split()
                lines += [
                    _prom_line("jarvis_load_1m", float(parts[0])),
                    _prom_line("jarvis_load_5m", float(parts[1])),
                    _prom_line("jarvis_load_15m", float(parts[2])),
                ]
        except Exception:
            pass
        return lines

    async def collect_redis(self) -> list[str]:
        lines = []
        if not self.redis:
            lines.append(_prom_line("jarvis_redis_up", 0))
            return lines
        try:
            info = await self.redis.info()
            lines += [
                _prom_line("jarvis_redis_up", 1),
                _prom_line(
                    "jarvis_redis_connected_clients", info.get("connected_clients", 0)
                ),
                _prom_line(
                    "jarvis_redis_used_memory_bytes", info.get("used_memory", 0)
                ),
                _prom_line(
                    "jarvis_redis_total_commands",
                    info.get("total_commands_processed", 0),
                ),
                _prom_line("jarvis_redis_keyspace_hits", info.get("keyspace_hits", 0)),
                _prom_line(
                    "jarvis_redis_keyspace_misses", info.get("keyspace_misses", 0)
                ),
            ]
            db_info = await self.redis.dbsize()
            lines.append(_prom_line("jarvis_redis_keys_total", db_info))
        except Exception:
            lines.append(_prom_line("jarvis_redis_up", 0))
        return lines

    async def collect_nodes(self) -> list[str]:
        lines = []
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=3)
        ) as sess:
            for node, url in NODES.items():
                t0 = time.time()
                try:
                    async with sess.get(f"{url}/v1/models") as r:
                        latency = (time.time() - t0) * 1000
                        up = 1 if r.status == 200 else 0
                        data = await r.json() if r.status == 200 else {}
                        model_count = len(data.get("data", []))
                except Exception:
                    up, latency, model_count = 0, 0.0, 0

                lbl = {"node": node}
                lines += [
                    _prom_line("jarvis_node_up", up, lbl),
                    _prom_line("jarvis_node_latency_ms", round(latency, 1), lbl),
                    _prom_line("jarvis_node_models_loaded", model_count, lbl),
                ]
        return lines

    async def collect_jarvis_metrics(self) -> list[str]:
        """Pull JARVIS-specific metrics from Redis."""
        lines = []
        if not self.redis:
            return lines
        try:
            counters = await self.redis.hgetall("jarvis:metrics:counters")
            for key, val in counters.items():
                safe_key = (
                    key.replace("{", "_")
                    .replace("}", "")
                    .replace(",", "_")
                    .replace("=", "_")
                )
                lines.append(f"jarvis_counter_{safe_key} {float(val)}")

            gauges = await self.redis.hgetall("jarvis:metrics:gauges")
            for key, val in gauges.items():
                safe_key = (
                    key.replace("{", "_")
                    .replace("}", "")
                    .replace(",", "_")
                    .replace("=", "_")
                )
                lines.append(f"jarvis_gauge_{safe_key} {float(val)}")
        except Exception as e:
            log.debug(f"Jarvis metrics collect: {e}")
        return lines

    async def generate_metrics(self) -> str:
        now = time.time()
        if self._cache and now - self._cache_ts < self._cache_ttl:
            return self._cache

        gpu, system, redis_m, nodes, jarvis_m = await asyncio.gather(
            self.collect_gpu(),
            self.collect_system(),
            self.collect_redis(),
            self.collect_nodes(),
            self.collect_jarvis_metrics(),
        )

        sections = [
            "# HELP jarvis_gpu_temp_celsius GPU temperature",
            "# TYPE jarvis_gpu_temp_celsius gauge",
            *gpu,
            "# HELP jarvis_mem_used_kb RAM used",
            "# TYPE jarvis_mem_used_kb gauge",
            *system,
            "# HELP jarvis_redis_up Redis availability",
            "# TYPE jarvis_redis_up gauge",
            *redis_m,
            "# HELP jarvis_node_up LLM node availability",
            "# TYPE jarvis_node_up gauge",
            *nodes,
            *jarvis_m,
            f"jarvis_exporter_scrape_ts {now}",
            "",
        ]

        self._cache = "\n".join(sections)
        self._cache_ts = now
        return self._cache

    async def handle_metrics(self, request: web.Request) -> web.Response:
        body = await self.generate_metrics()
        return web.Response(text=body, content_type="text/plain")

    async def handle_health(self, request: web.Request) -> web.Response:
        return web.Response(text='{"status":"ok"}', content_type="application/json")

    async def run(self):
        app = web.Application()
        app.router.add_get("/metrics", self.handle_metrics)
        app.router.add_get("/health", self.handle_health)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.port)
        await site.start()
        log.info(f"Node exporter running on :{self.port}/metrics")
        return runner


async def main():
    import sys

    exporter = NodeExporter()
    await exporter.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"

    if cmd == "dump":
        metrics = await exporter.generate_metrics()
        print(metrics)

    elif cmd == "serve":
        runner = await exporter.run()
        print(f"Exporter running on :{exporter.port}/metrics (Ctrl+C to stop)")
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
