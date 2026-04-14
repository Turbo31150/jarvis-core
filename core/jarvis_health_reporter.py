#!/usr/bin/env python3
"""
jarvis_health_reporter — Aggregated health report generator (JSON/Markdown/HTML)
Collects metrics from Redis, GPU, services, cluster nodes into unified snapshot
"""

import asyncio
import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.health_reporter")

REPORT_DIR = Path("/home/turbo/IA/Core/jarvis/data/reports")
NODES = {
    "M1": "http://127.0.0.1:1234",
    "M2": "http://192.168.1.26:1234",
}


@dataclass
class NodeHealth:
    name: str
    url: str
    reachable: bool
    latency_ms: float = 0.0
    models: list[str] = field(default_factory=list)
    error: str = ""


@dataclass
class GPUInfo:
    idx: int
    name: str
    temp_c: int
    power_w: float
    vram_used_mb: int
    vram_total_mb: int
    util_pct: int

    @property
    def vram_pct(self) -> float:
        if not self.vram_total_mb:
            return 0.0
        return round(self.vram_used_mb / self.vram_total_mb * 100, 1)

    @property
    def status(self) -> str:
        if self.temp_c >= 85:
            return "CRITICAL"
        if self.temp_c >= 75:
            return "WARN"
        return "OK"


@dataclass
class HealthReport:
    ts: float = field(default_factory=time.time)
    gpus: list[GPUInfo] = field(default_factory=list)
    nodes: list[NodeHealth] = field(default_factory=list)
    redis_ok: bool = False
    redis_keys: int = 0
    services: dict[str, str] = field(default_factory=dict)  # name→status
    system: dict[str, Any] = field(default_factory=dict)

    @property
    def overall_status(self) -> str:
        if any(g.status == "CRITICAL" for g in self.gpus):
            return "CRITICAL"
        if not self.redis_ok:
            return "DEGRADED"
        if any(n.name == "M1" and not n.reachable for n in self.nodes):
            return "DEGRADED"
        if any(g.status == "WARN" for g in self.gpus):
            return "WARN"
        return "OK"

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "ts_human": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.ts)),
            "overall_status": self.overall_status,
            "gpus": [
                {
                    "idx": g.idx,
                    "name": g.name,
                    "temp_c": g.temp_c,
                    "power_w": g.power_w,
                    "vram_used_mb": g.vram_used_mb,
                    "vram_total_mb": g.vram_total_mb,
                    "vram_pct": g.vram_pct,
                    "util_pct": g.util_pct,
                    "status": g.status,
                }
                for g in self.gpus
            ],
            "nodes": [
                {
                    "name": n.name,
                    "reachable": n.reachable,
                    "latency_ms": n.latency_ms,
                    "models": n.models,
                    "error": n.error,
                }
                for n in self.nodes
            ],
            "redis": {"ok": self.redis_ok, "keys": self.redis_keys},
            "services": self.services,
            "system": self.system,
        }

    def to_markdown(self) -> str:
        icon = {"OK": "✅", "WARN": "⚠️", "DEGRADED": "⚠️", "CRITICAL": "🔴"}.get(
            self.overall_status, "❓"
        )
        ts_h = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.ts))
        lines = [
            f"# JARVIS Health Report — {ts_h}",
            f"**Overall: {icon} {self.overall_status}**",
            "",
            "## GPUs",
            f"{'#':<3} {'Name':<20} {'Temp':>5} {'Power':>7} {'VRAM':>12} {'Util':>5} {'Status':<8}",
            "-" * 62,
        ]
        for g in self.gpus:
            vram_str = f"{g.vram_used_mb}/{g.vram_total_mb}MB"
            lines.append(
                f"{g.idx:<3} {g.name:<20} {g.temp_c:>4}°C {g.power_w:>6.0f}W {vram_str:>12} {g.util_pct:>4}% {g.status:<8}"
            )
        lines.append("")
        lines.append("## Cluster Nodes")
        for n in self.nodes:
            status = "✅" if n.reachable else "❌"
            models_str = ", ".join(n.models[:3]) if n.models else "—"
            lines.append(f"- **{n.name}** {status} {n.latency_ms:.0f}ms | {models_str}")
        lines.append("")
        lines.append("## Services")
        for svc, state in self.services.items():
            icon_s = "✅" if state == "active" else "❌"
            lines.append(f"- {icon_s} `{svc}` — {state}")
        lines.append("")
        lines.append("## System")
        for k, v in self.system.items():
            lines.append(f"- **{k}**: {v}")
        return "\n".join(lines)


class HealthReporter:
    def __init__(self):
        self.redis: aioredis.Redis | None = None

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    async def collect(self) -> HealthReport:
        report = HealthReport()
        gpus_task = asyncio.create_task(self._collect_gpus())
        nodes_task = asyncio.create_task(self._collect_nodes())
        sys_task = asyncio.create_task(self._collect_system())
        svc_task = asyncio.create_task(self._collect_services())

        report.gpus = await gpus_task
        report.nodes = await nodes_task
        report.system = await sys_task
        report.services = await svc_task

        if self.redis:
            try:
                report.redis_ok = True
                report.redis_keys = await self.redis.dbsize()
            except Exception:
                report.redis_ok = False

        return report

    async def _collect_gpus(self) -> list[GPUInfo]:
        gpus = []
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
                if len(p) >= 7:
                    gpus.append(
                        GPUInfo(
                            idx=int(p[0]),
                            name=p[1],
                            temp_c=int(float(p[2])),
                            power_w=float(p[3]),
                            vram_used_mb=int(float(p[4])),
                            vram_total_mb=int(float(p[5])),
                            util_pct=int(float(p[6])),
                        )
                    )
        except Exception as e:
            log.warning(f"GPU collect error: {e}")
        return gpus

    async def _collect_nodes(self) -> list[NodeHealth]:
        nodes = []
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=3)
        ) as sess:
            for name, url in NODES.items():
                t0 = time.time()
                try:
                    async with sess.get(f"{url}/v1/models") as r:
                        latency = round((time.time() - t0) * 1000, 1)
                        if r.status == 200:
                            data = await r.json()
                            models = [m["id"] for m in data.get("data", [])[:5]]
                            nodes.append(
                                NodeHealth(
                                    name=name,
                                    url=url,
                                    reachable=True,
                                    latency_ms=latency,
                                    models=models,
                                )
                            )
                        else:
                            nodes.append(
                                NodeHealth(
                                    name=name,
                                    url=url,
                                    reachable=False,
                                    error=f"HTTP {r.status}",
                                )
                            )
                except Exception as e:
                    nodes.append(
                        NodeHealth(
                            name=name, url=url, reachable=False, error=str(e)[:60]
                        )
                    )
        return nodes

    async def _collect_services(self) -> dict[str, str]:
        services = {}
        svc_list = ["redis", "jarvis-gpu-oc", "lmstudio"]
        for svc in svc_list:
            try:
                r = subprocess.run(
                    ["systemctl", "is-active", svc],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                services[svc] = r.stdout.strip()
            except Exception:
                services[svc] = "unknown"
        return services

    async def _collect_system(self) -> dict:
        result = {}
        try:
            # RAM
            with open("/proc/meminfo") as f:
                mem = {}
                for line in f:
                    k, v = line.split(":")
                    mem[k.strip()] = v.strip()
            total_kb = int(mem.get("MemTotal", "0 kB").split()[0])
            avail_kb = int(mem.get("MemAvailable", "0 kB").split()[0])
            used_mb = (total_kb - avail_kb) // 1024
            total_mb = total_kb // 1024
            result["ram"] = (
                f"{used_mb}/{total_mb}MB ({round(used_mb / total_mb * 100)}%)"
            )

            # Load average
            with open("/proc/loadavg") as f:
                la = f.read().split()
                result["load_avg"] = f"{la[0]} {la[1]} {la[2]}"

            # Uptime
            with open("/proc/uptime") as f:
                secs = float(f.read().split()[0])
                result["uptime"] = f"{int(secs // 3600)}h {int((secs % 3600) // 60)}m"
        except Exception as e:
            log.debug(f"System collect: {e}")
        return result

    async def save(self, report: HealthReport, fmt: str = "json") -> Path:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        ts_str = time.strftime("%Y%m%d_%H%M%S", time.localtime(report.ts))
        if fmt == "markdown":
            path = REPORT_DIR / f"health_{ts_str}.md"
            path.write_text(report.to_markdown())
        else:
            path = REPORT_DIR / f"health_{ts_str}.json"
            path.write_text(json.dumps(report.to_dict(), indent=2))
        return path


async def main():
    import sys

    reporter = HealthReporter()
    await reporter.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"

    if cmd == "report":
        report = await reporter.collect()
        icons = {"OK": "✅", "WARN": "⚠️", "DEGRADED": "⚠️", "CRITICAL": "🔴"}
        print(
            f"{icons.get(report.overall_status, '?')} Overall: {report.overall_status}"
        )
        print(
            f"GPUs: {len(report.gpus)} | Nodes: {sum(1 for n in report.nodes if n.reachable)}/{len(report.nodes)} up | Redis: {'✅' if report.redis_ok else '❌'}"
        )
        for g in report.gpus:
            print(
                f"  GPU{g.idx} {g.temp_c}°C {g.power_w:.0f}W {g.vram_used_mb}/{g.vram_total_mb}MB {g.status}"
            )

    elif cmd == "json":
        report = await reporter.collect()
        print(json.dumps(report.to_dict(), indent=2))

    elif cmd == "markdown":
        report = await reporter.collect()
        print(report.to_markdown())

    elif cmd == "save":
        report = await reporter.collect()
        fmt = sys.argv[2] if len(sys.argv) > 2 else "json"
        path = await reporter.save(report, fmt)
        print(f"Saved: {path}")


if __name__ == "__main__":
    asyncio.run(main())
