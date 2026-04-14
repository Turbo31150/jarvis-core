#!/usr/bin/env python3
"""
jarvis_system_profiler — System resource profiling and performance snapshots
CPU, RAM, GPU, disk, network metrics with trend analysis and Redis export
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.system_profiler")

PROFILE_FILE = Path("/home/turbo/IA/Core/jarvis/data/system_profiles.jsonl")
REDIS_PREFIX = "jarvis:profiler:"


@dataclass
class CPUSnapshot:
    cpu_count: int
    usage_pct: float  # overall
    per_core: list[float]  # per-core usage
    load_avg_1m: float
    load_avg_5m: float
    load_avg_15m: float
    ctx_switches: int = 0
    interrupts: int = 0

    def to_dict(self) -> dict:
        return {
            "cpu_count": self.cpu_count,
            "usage_pct": round(self.usage_pct, 1),
            "per_core": [round(p, 1) for p in self.per_core],
            "load_avg": [
                round(self.load_avg_1m, 2),
                round(self.load_avg_5m, 2),
                round(self.load_avg_15m, 2),
            ],
        }


@dataclass
class MemSnapshot:
    total_mb: int
    used_mb: int
    free_mb: int
    cached_mb: int = 0
    swap_total_mb: int = 0
    swap_used_mb: int = 0

    @property
    def used_pct(self) -> float:
        return self.used_mb / max(self.total_mb, 1) * 100

    def to_dict(self) -> dict:
        return {
            "total_mb": self.total_mb,
            "used_mb": self.used_mb,
            "free_mb": self.free_mb,
            "used_pct": round(self.used_pct, 1),
            "cached_mb": self.cached_mb,
            "swap_used_mb": self.swap_used_mb,
        }


@dataclass
class DiskSnapshot:
    device: str
    mount: str
    total_gb: float
    used_gb: float
    free_gb: float
    read_mb_s: float = 0.0
    write_mb_s: float = 0.0

    @property
    def used_pct(self) -> float:
        return self.used_gb / max(self.total_gb, 1) * 100

    def to_dict(self) -> dict:
        return {
            "device": self.device,
            "mount": self.mount,
            "total_gb": round(self.total_gb, 1),
            "used_gb": round(self.used_gb, 1),
            "free_gb": round(self.free_gb, 1),
            "used_pct": round(self.used_pct, 1),
        }


@dataclass
class NetSnapshot:
    interface: str
    bytes_sent: int
    bytes_recv: int
    send_mb_s: float = 0.0
    recv_mb_s: float = 0.0
    packets_sent: int = 0
    packets_recv: int = 0
    errors: int = 0

    def to_dict(self) -> dict:
        return {
            "interface": self.interface,
            "send_mb_s": round(self.send_mb_s, 2),
            "recv_mb_s": round(self.recv_mb_s, 2),
            "bytes_sent": self.bytes_sent,
            "bytes_recv": self.bytes_recv,
            "errors": self.errors,
        }


@dataclass
class GPUSnapshot:
    gpu_id: int
    name: str
    temperature_c: float
    utilization_pct: float
    vram_used_mb: int
    vram_total_mb: int
    power_w: float = 0.0
    fan_pct: float = 0.0

    @property
    def vram_pct(self) -> float:
        return self.vram_used_mb / max(self.vram_total_mb, 1) * 100

    def to_dict(self) -> dict:
        return {
            "gpu_id": self.gpu_id,
            "name": self.name,
            "temperature_c": self.temperature_c,
            "utilization_pct": self.utilization_pct,
            "vram_used_mb": self.vram_used_mb,
            "vram_total_mb": self.vram_total_mb,
            "vram_pct": round(self.vram_pct, 1),
            "power_w": self.power_w,
        }


@dataclass
class SystemSnapshot:
    ts: float = field(default_factory=time.time)
    hostname: str = ""
    cpu: CPUSnapshot | None = None
    mem: MemSnapshot | None = None
    disks: list[DiskSnapshot] = field(default_factory=list)
    net: list[NetSnapshot] = field(default_factory=list)
    gpus: list[GPUSnapshot] = field(default_factory=list)
    process_count: int = 0
    uptime_s: float = 0.0

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "hostname": self.hostname,
            "cpu": self.cpu.to_dict() if self.cpu else {},
            "mem": self.mem.to_dict() if self.mem else {},
            "disks": [d.to_dict() for d in self.disks],
            "net": [n.to_dict() for n in self.net],
            "gpus": [g.to_dict() for g in self.gpus],
            "process_count": self.process_count,
            "uptime_s": round(self.uptime_s, 0),
        }


def _read_cpu() -> CPUSnapshot:
    try:
        import psutil

        cpu_pct = psutil.cpu_percent(interval=0.1)
        per_core = psutil.cpu_percent(percpu=True)
        la = os.getloadavg()
        return CPUSnapshot(
            cpu_count=psutil.cpu_count(),
            usage_pct=cpu_pct,
            per_core=per_core,
            load_avg_1m=la[0],
            load_avg_5m=la[1],
            load_avg_15m=la[2],
        )
    except ImportError:
        la = os.getloadavg()
        cpu_count = os.cpu_count() or 1
        return CPUSnapshot(
            cpu_count=cpu_count,
            usage_pct=la[0] / cpu_count * 100,
            per_core=[],
            load_avg_1m=la[0],
            load_avg_5m=la[1],
            load_avg_15m=la[2],
        )


def _read_mem() -> MemSnapshot:
    try:
        import psutil

        vm = psutil.virtual_memory()
        sw = psutil.swap_memory()
        return MemSnapshot(
            total_mb=vm.total // 1024 // 1024,
            used_mb=vm.used // 1024 // 1024,
            free_mb=vm.available // 1024 // 1024,
            cached_mb=getattr(vm, "cached", 0) // 1024 // 1024,
            swap_total_mb=sw.total // 1024 // 1024,
            swap_used_mb=sw.used // 1024 // 1024,
        )
    except ImportError:
        try:
            with open("/proc/meminfo") as f:
                lines = {
                    k.strip(":"): int(v.split()[0])
                    for k, v in (line.split(":", 1) for line in f if ":" in line)
                }
            total = lines.get("MemTotal", 0) // 1024
            free = lines.get("MemAvailable", 0) // 1024
            return MemSnapshot(total_mb=total, used_mb=total - free, free_mb=free)
        except Exception:
            return MemSnapshot(total_mb=0, used_mb=0, free_mb=0)


def _read_disk() -> list[DiskSnapshot]:
    snaps = []
    try:
        import psutil

        for part in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(part.mountpoint)
                snaps.append(
                    DiskSnapshot(
                        device=part.device,
                        mount=part.mountpoint,
                        total_gb=usage.total / 1e9,
                        used_gb=usage.used / 1e9,
                        free_gb=usage.free / 1e9,
                    )
                )
            except Exception:
                pass
    except ImportError:
        try:
            result = os.statvfs("/")
            total = result.f_blocks * result.f_frsize / 1e9
            free = result.f_bfree * result.f_frsize / 1e9
            snaps.append(DiskSnapshot("/dev/sda", "/", total, total - free, free))
        except Exception:
            pass
    return snaps[:4]  # top 4 mounts


def _read_gpu() -> list[GPUSnapshot]:
    snaps = []
    try:
        import subprocess

        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in result.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 6:
                snaps.append(
                    GPUSnapshot(
                        gpu_id=int(parts[0]),
                        name=parts[1],
                        temperature_c=float(parts[2]),
                        utilization_pct=float(parts[3]),
                        vram_used_mb=int(parts[4]),
                        vram_total_mb=int(parts[5]),
                        power_w=float(parts[6])
                        if len(parts) > 6 and parts[6] != "[N/A]"
                        else 0.0,
                    )
                )
    except Exception:
        pass
    return snaps


class SystemProfiler:
    def __init__(
        self,
        history_size: int = 300,
        persist: bool = True,
    ):
        self.redis: aioredis.Redis | None = None
        self._history: list[SystemSnapshot] = []
        self._history_size = history_size
        self._persist = persist
        self._auto_task: asyncio.Task | None = None
        self._stats: dict[str, int] = {"snapshots": 0}
        if persist:
            PROFILE_FILE.parent.mkdir(parents=True, exist_ok=True)

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def snapshot(self) -> SystemSnapshot:
        snap = SystemSnapshot(
            hostname=os.uname().nodename,
            cpu=_read_cpu(),
            mem=_read_mem(),
            disks=_read_disk(),
            gpus=_read_gpu(),
            uptime_s=time.time()
            - (
                float(Path("/proc/uptime").read_text().split()[0])
                if Path("/proc/uptime").exists()
                else 0
            ),
        )
        try:
            import psutil

            snap.process_count = len(psutil.pids())
        except Exception:
            pass

        self._history.append(snap)
        if len(self._history) > self._history_size:
            self._history.pop(0)

        self._stats["snapshots"] += 1

        if self._persist:
            try:
                with open(PROFILE_FILE, "a") as f:
                    f.write(json.dumps(snap.to_dict()) + "\n")
            except Exception:
                pass

        if self.redis:
            asyncio.create_task(
                self.redis.setex(
                    f"{REDIS_PREFIX}latest",
                    300,
                    json.dumps(snap.to_dict()),
                )
            )

        return snap

    def start_auto(self, interval_s: float = 15.0):
        async def _loop():
            while True:
                self.snapshot()
                await asyncio.sleep(interval_s)

        self._auto_task = asyncio.create_task(_loop())

    def stop_auto(self):
        if self._auto_task:
            self._auto_task.cancel()

    def latest(self) -> SystemSnapshot | None:
        return self._history[-1] if self._history else None

    def trend(self, field_path: str, n: int = 10) -> list[float]:
        """Extract trend for a dotted field path e.g. 'cpu.usage_pct'."""
        values = []
        for snap in self._history[-n:]:
            d = snap.to_dict()
            parts = field_path.split(".")
            val = d
            try:
                for p in parts:
                    if isinstance(val, list):
                        val = val[int(p)]
                    else:
                        val = val[p]
                values.append(float(val))
            except Exception:
                pass
        return values

    def stats(self) -> dict:
        return {
            **self._stats,
            "history_size": len(self._history),
            "auto_running": self._auto_task is not None and not self._auto_task.done(),
        }


def build_jarvis_system_profiler() -> SystemProfiler:
    return SystemProfiler(history_size=300, persist=True)


async def main():
    import sys

    profiler = build_jarvis_system_profiler()
    await profiler.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "snapshot"

    if cmd == "snapshot":
        print("Taking system snapshot...")
        snap = profiler.snapshot()
        d = snap.to_dict()

        if d["cpu"]:
            cpu = d["cpu"]
            print(
                f"CPU: {cpu['usage_pct']}% ({cpu['cpu_count']} cores) load={cpu['load_avg']}"
            )
        if d["mem"]:
            mem = d["mem"]
            print(f"RAM: {mem['used_mb']}/{mem['total_mb']}MB ({mem['used_pct']:.1f}%)")
        for disk in d["disks"][:2]:
            print(
                f"Disk {disk['mount']}: {disk['used_gb']:.1f}/{disk['total_gb']:.1f}GB ({disk['used_pct']:.1f}%)"
            )
        for gpu in d["gpus"]:
            print(
                f"GPU{gpu['gpu_id']} {gpu['name']}: "
                f"{gpu['temperature_c']}°C "
                f"util={gpu['utilization_pct']}% "
                f"VRAM {gpu['vram_used_mb']}/{gpu['vram_total_mb']}MB"
            )
        print(f"Processes: {d['process_count']}")
        print(f"Uptime: {d['uptime_s'] / 3600:.1f}h")

    elif cmd == "watch":
        interval = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0
        print(f"Watching every {interval}s (Ctrl+C to stop)...")
        while True:
            snap = profiler.snapshot()
            cpu_pct = snap.cpu.usage_pct if snap.cpu else 0
            mem_pct = snap.mem.used_pct if snap.mem else 0
            gpu_info = " ".join(f"GPU{g.gpu_id}:{g.temperature_c}°C" for g in snap.gpus)
            print(
                f"[{time.strftime('%H:%M:%S')}] CPU:{cpu_pct:.0f}% RAM:{mem_pct:.0f}% {gpu_info}"
            )
            await asyncio.sleep(interval)

    elif cmd == "stats":
        print(json.dumps(profiler.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
