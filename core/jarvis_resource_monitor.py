#!/usr/bin/env python3
"""
jarvis_resource_monitor — System resource monitoring with trend detection
CPU, RAM, disk, GPU, network — rolling averages, anomaly detection, Redis metrics
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.resource_monitor")

REDIS_PREFIX = "jarvis:res:"
POLL_INTERVAL_S = 10.0
HISTORY_POINTS = 60  # keep last 60 samples


@dataclass
class Metric:
    name: str
    value: float
    unit: str
    ts: float = field(default_factory=time.time)
    tags: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "value": round(self.value, 2),
            "unit": self.unit,
            "ts": self.ts,
            "tags": self.tags,
        }


@dataclass
class ResourceSnapshot:
    ts: float = field(default_factory=time.time)
    cpu_pct: float = 0.0
    ram_used_mb: float = 0.0
    ram_total_mb: float = 0.0
    ram_pct: float = 0.0
    disk_used_gb: float = 0.0
    disk_total_gb: float = 0.0
    disk_pct: float = 0.0
    load_1m: float = 0.0
    load_5m: float = 0.0
    gpus: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "cpu_pct": round(self.cpu_pct, 1),
            "ram_used_mb": round(self.ram_used_mb),
            "ram_total_mb": round(self.ram_total_mb),
            "ram_pct": round(self.ram_pct, 1),
            "disk_used_gb": round(self.disk_used_gb, 1),
            "disk_total_gb": round(self.disk_total_gb, 1),
            "disk_pct": round(self.disk_pct, 1),
            "load_1m": round(self.load_1m, 2),
            "load_5m": round(self.load_5m, 2),
            "gpus": self.gpus,
        }


def _read_cpu() -> float:
    try:
        with open("/proc/stat") as f:
            line = f.readline()
        vals = [int(x) for x in line.split()[1:]]
        idle = vals[3]
        total = sum(vals)
        # Second read for delta
        time.sleep(0.1)
        with open("/proc/stat") as f:
            line2 = f.readline()
        vals2 = [int(x) for x in line2.split()[1:]]
        idle2 = vals2[3]
        total2 = sum(vals2)
        delta_idle = idle2 - idle
        delta_total = total2 - total
        return round(100.0 * (1 - delta_idle / max(delta_total, 1)), 1)
    except Exception:
        return 0.0


def _read_ram() -> tuple[float, float]:
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":")
                info[k.strip()] = int(v.strip().split()[0])  # kB
        total = info.get("MemTotal", 0) / 1024
        available = info.get("MemAvailable", 0) / 1024
        used = total - available
        return used, total
    except Exception:
        return 0.0, 0.0


def _read_disk(path: str = "/") -> tuple[float, float]:
    try:
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize / (1024**3)
        free = st.f_bavail * st.f_frsize / (1024**3)
        used = total - free
        return used, total
    except Exception:
        return 0.0, 0.0


def _read_load() -> tuple[float, float]:
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
        return float(parts[0]), float(parts[1])
    except Exception:
        return 0.0, 0.0


def _read_gpus() -> list[dict]:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,temperature.gpu,memory.used,memory.total,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        gpus = []
        for line in result.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 6:
                gpus.append(
                    {
                        "index": int(parts[0]),
                        "name": parts[1],
                        "temp_c": float(parts[2]),
                        "vram_used_mb": float(parts[3]),
                        "vram_total_mb": float(parts[4]),
                        "util_pct": float(parts[5]),
                    }
                )
        return gpus
    except Exception:
        return []


class ResourceMonitor:
    def __init__(self, poll_interval_s: float = POLL_INTERVAL_S):
        self.redis: aioredis.Redis | None = None
        self.poll_interval_s = poll_interval_s
        self._history: list[ResourceSnapshot] = []
        self._monitor_task: asyncio.Task | None = None
        self._thresholds = {
            "cpu_pct": 90.0,
            "ram_pct": 85.0,
            "disk_pct": 90.0,
            "gpu_temp_c": 85.0,
        }
        self._alert_callbacks: list = []

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def set_threshold(self, metric: str, value: float):
        self._thresholds[metric] = value

    def on_threshold(self, callback):
        self._alert_callbacks.append(callback)

    async def collect(self) -> ResourceSnapshot:
        snap = ResourceSnapshot()
        snap.cpu_pct = _read_cpu()
        snap.ram_used_mb, snap.ram_total_mb = _read_ram()
        snap.ram_pct = snap.ram_used_mb / max(snap.ram_total_mb, 1) * 100
        snap.disk_used_gb, snap.disk_total_gb = _read_disk("/")
        snap.disk_pct = snap.disk_used_gb / max(snap.disk_total_gb, 1) * 100
        snap.load_1m, snap.load_5m = _read_load()
        snap.gpus = _read_gpus()

        self._history.append(snap)
        if len(self._history) > HISTORY_POINTS:
            self._history.pop(0)

        await self._check_thresholds(snap)
        await self._push_redis(snap)
        return snap

    async def _check_thresholds(self, snap: ResourceSnapshot):
        checks = {
            "cpu_pct": snap.cpu_pct,
            "ram_pct": snap.ram_pct,
            "disk_pct": snap.disk_pct,
        }
        for gpu in snap.gpus:
            checks[f"gpu_{gpu['index']}_temp_c"] = gpu["temp_c"]

        for metric, value in checks.items():
            base_metric = re.sub(r"_\d+_", "_", metric)
            threshold = self._thresholds.get(base_metric, self._thresholds.get(metric))
            if threshold and value > threshold:
                log.warning(f"Threshold exceeded: {metric}={value:.1f} > {threshold}")
                for cb in self._alert_callbacks:
                    try:
                        cb(metric, value, threshold)
                    except Exception:
                        pass

    async def _push_redis(self, snap: ResourceSnapshot):
        if not self.redis:
            return
        d = snap.to_dict()
        await self.redis.setex(f"{REDIS_PREFIX}latest", 120, json.dumps(d))
        await self.redis.lpush(f"{REDIS_PREFIX}history", json.dumps(d))
        await self.redis.ltrim(f"{REDIS_PREFIX}history", 0, HISTORY_POINTS - 1)
        # Individual metrics for dashboards
        metrics = {
            "cpu_pct": snap.cpu_pct,
            "ram_pct": snap.ram_pct,
            "disk_pct": snap.disk_pct,
            "load_1m": snap.load_1m,
        }
        for k, v in metrics.items():
            await self.redis.set(f"{REDIS_PREFIX}{k}", round(v, 1))

    async def start(self):
        async def loop():
            while True:
                try:
                    await self.collect()
                except Exception as e:
                    log.error(f"Monitor error: {e}")
                await asyncio.sleep(self.poll_interval_s)

        self._monitor_task = asyncio.create_task(loop())

    def stop(self):
        if self._monitor_task:
            self._monitor_task.cancel()

    def trend(self, metric: str, window: int = 10) -> dict:
        """Compute trend (slope) for a metric over last N samples."""
        if len(self._history) < 2:
            return {"metric": metric, "trend": "insufficient_data"}

        samples = self._history[-min(window, len(self._history)) :]
        values = []
        for s in samples:
            v = getattr(s, metric, None)
            if v is not None:
                values.append(v)

        if len(values) < 2:
            return {"metric": metric, "trend": "no_data"}

        avg = sum(values) / len(values)
        slope = (values[-1] - values[0]) / max(len(values) - 1, 1)
        direction = (
            "rising" if slope > 0.5 else ("falling" if slope < -0.5 else "stable")
        )

        return {
            "metric": metric,
            "current": round(values[-1], 1),
            "avg": round(avg, 1),
            "slope": round(slope, 2),
            "direction": direction,
            "samples": len(values),
        }

    def latest(self) -> ResourceSnapshot | None:
        return self._history[-1] if self._history else None

    def stats(self) -> dict:
        snap = self.latest()
        if not snap:
            return {"status": "no_data"}
        return {
            "cpu_pct": snap.cpu_pct,
            "ram_pct": round(snap.ram_pct, 1),
            "disk_pct": round(snap.disk_pct, 1),
            "load_1m": snap.load_1m,
            "gpu_count": len(snap.gpus),
            "history_points": len(self._history),
        }


async def main():
    import sys

    monitor = ResourceMonitor(poll_interval_s=2.0)
    monitor.on_threshold(lambda m, v, t: print(f"  ⚠️ THRESHOLD: {m}={v:.1f} > {t}"))
    await monitor.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "snapshot"

    if cmd == "snapshot":
        snap = await monitor.collect()
        d = snap.to_dict()
        print(
            f"CPU: {d['cpu_pct']}%  RAM: {d['ram_used_mb']:.0f}/{d['ram_total_mb']:.0f}MB ({d['ram_pct']}%)"
        )
        print(f"Disk: {d['disk_used_gb']}GB/{d['disk_total_gb']}GB ({d['disk_pct']}%)")
        print(f"Load: {d['load_1m']} / {d['load_5m']}")
        if d["gpus"]:
            for g in d["gpus"]:
                print(
                    f"GPU{g['index']}: {g['temp_c']}°C  {g['vram_used_mb']:.0f}/{g['vram_total_mb']:.0f}MB  {g['util_pct']}%"
                )

    elif cmd == "watch":
        print("Monitoring every 2s (Ctrl+C to stop)...")
        await monitor.start()
        try:
            while True:
                await asyncio.sleep(5)
                snap = monitor.latest()
                if snap:
                    print(
                        f"  CPU={snap.cpu_pct:.1f}% RAM={snap.ram_pct:.1f}% Load={snap.load_1m:.2f}"
                    )
        except KeyboardInterrupt:
            monitor.stop()

    elif cmd == "stats":
        await monitor.collect()
        print(json.dumps(monitor.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
