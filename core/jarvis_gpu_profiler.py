#!/usr/bin/env python3
"""
jarvis_gpu_profiler — Detailed GPU profiling: PCIe bandwidth, compute efficiency
Measures inference-time metrics, builds perf fingerprint per model+config
"""

import asyncio
import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.gpu_profiler")

PROFILE_DB = Path("/home/turbo/IA/Core/jarvis/data/gpu_profiles.json")
REDIS_KEY = "jarvis:gpu_profiler"

# RTX 3080 theoretical peaks
GPU_SPECS = {
    4: {  # RTX 3080
        "name": "RTX 3080",
        "fp16_tflops": 29.8,
        "mem_bw_gbs": 760.0,  # GB/s
        "vram_gb": 10.0,
        "pcie_gen": 4,
        "pcie_lanes": 16,
        "pcie_bw_gbs": 32.0,
    },
    0: {  # RTX 2060
        "name": "RTX 2060",
        "fp16_tflops": 13.4,
        "mem_bw_gbs": 336.0,
        "vram_gb": 12.0,
        "pcie_gen": 3,
        "pcie_lanes": 4,
        "pcie_bw_gbs": 4.0,
    },
    1: {
        "name": "GTX 1660S",
        "fp16_tflops": 6.0,
        "mem_bw_gbs": 192.0,
        "vram_gb": 6.0,
        "pcie_gen": 3,
        "pcie_lanes": 1,
        "pcie_bw_gbs": 1.0,
    },
    2: {
        "name": "GTX 1660S",
        "fp16_tflops": 6.0,
        "mem_bw_gbs": 192.0,
        "vram_gb": 6.0,
        "pcie_gen": 3,
        "pcie_lanes": 1,
        "pcie_bw_gbs": 1.0,
    },
    3: {
        "name": "GTX 1660S",
        "fp16_tflops": 6.0,
        "mem_bw_gbs": 192.0,
        "vram_gb": 6.0,
        "pcie_gen": 3,
        "pcie_lanes": 1,
        "pcie_bw_gbs": 1.0,
    },
}


@dataclass
class GPUProfile:
    gpu_idx: int
    name: str
    sample_count: int = 0
    avg_temp: float = 0.0
    avg_power_w: float = 0.0
    avg_util: float = 0.0
    avg_mem_util: float = 0.0
    peak_temp: int = 0
    peak_power_w: float = 0.0
    # Inference metrics (when tps data available)
    avg_tps: float = 0.0
    compute_efficiency: float = 0.0  # actual_tps / theoretical_max_tps
    memory_efficiency: float = 0.0  # actual_mem_bw / theoretical_mem_bw
    profiles: list[dict] = field(default_factory=list)


def query_nvml() -> list[dict]:
    """Query detailed GPU metrics via nvidia-smi."""
    r = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=index,name,temperature.gpu,power.draw,power.limit,"
            "utilization.gpu,utilization.memory,memory.used,memory.total,"
            "clocks.current.graphics,clocks.current.memory",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
    )
    results = []
    for line in r.stdout.strip().split("\n"):
        if not line.strip():
            continue
        p = [x.strip() for x in line.split(",")]
        try:
            results.append(
                {
                    "idx": int(p[0]),
                    "name": p[1],
                    "temp": int(p[2]),
                    "power_draw": float(p[3]),
                    "power_limit": float(p[4]),
                    "util_gpu": int(p[5]),
                    "util_mem": int(p[6]),
                    "mem_used": int(p[7]),
                    "mem_total": int(p[8]),
                    "clock_core": int(p[9]),
                    "clock_mem": int(p[10]),
                }
            )
        except (ValueError, IndexError):
            continue
    return results


def compute_efficiency(gpu_idx: int, tps: float, power_w: float) -> dict:
    """
    Estimate compute and memory efficiency for given tps at given power.
    Reference: RTX 3080 at 320W achieves ~68 tok/s for 9B Q4 model.
    """
    spec = GPU_SPECS.get(gpu_idx, {})
    if not spec:
        return {}

    # Rough model: 68 tok/s at 320W baseline for 3080
    baseline_tps = 68.0 if gpu_idx == 4 else 30.0
    baseline_power = 320.0 if gpu_idx == 4 else 120.0
    theoretical_tps_at_power = baseline_tps * (power_w / baseline_power)

    compute_eff = tps / theoretical_tps_at_power if theoretical_tps_at_power > 0 else 0
    perf_watt = tps / power_w if power_w > 0 else 0

    return {
        "tps": round(tps, 1),
        "power_w": round(power_w, 1),
        "theoretical_tps": round(theoretical_tps_at_power, 1),
        "compute_efficiency_pct": round(compute_eff * 100, 1),
        "perf_watt": round(perf_watt, 4),
        "mem_bw_gbs": spec.get("mem_bw_gbs", 0),
        "pcie_bw_gbs": spec.get("pcie_bw_gbs", 0),
    }


class GPUProfiler:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self.profiles: dict[int, list[dict]] = {}
        self._load_db()

    def _load_db(self):
        if PROFILE_DB.exists():
            try:
                data = json.loads(PROFILE_DB.read_text())
                self.profiles = {int(k): v for k, v in data.items()}
            except Exception:
                self.profiles = {}

    def _save_db(self):
        PROFILE_DB.parent.mkdir(parents=True, exist_ok=True)
        PROFILE_DB.write_text(json.dumps(self.profiles, indent=2))

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def snapshot(self, tps_map: dict[int, float] | None = None) -> list[dict]:
        stats = query_nvml()
        ts = time.time()
        tps_map = tps_map or {}

        for s in stats:
            idx = s["idx"]
            entry = {
                "ts": ts,
                **s,
                "tps": tps_map.get(idx, 0.0),
            }
            if idx not in self.profiles:
                self.profiles[idx] = []
            self.profiles[idx].append(entry)
            # Keep last 1000 samples
            if len(self.profiles[idx]) > 1000:
                self.profiles[idx] = self.profiles[idx][-1000:]

        self._save_db()
        return stats

    def summarize(self, gpu_idx: int, last_n: int = 100) -> dict:
        samples = self.profiles.get(gpu_idx, [])[-last_n:]
        if not samples:
            return {"gpu": gpu_idx, "error": "no data"}

        spec = GPU_SPECS.get(gpu_idx, {})
        avg_temp = sum(s["temp"] for s in samples) / len(samples)
        avg_power = sum(s["power_draw"] for s in samples) / len(samples)
        avg_util = sum(s["util_gpu"] for s in samples) / len(samples)
        tps_samples = [s["tps"] for s in samples if s["tps"] > 0]
        avg_tps = sum(tps_samples) / len(tps_samples) if tps_samples else 0

        eff = compute_efficiency(gpu_idx, avg_tps, avg_power) if avg_tps > 0 else {}

        return {
            "gpu": gpu_idx,
            "name": spec.get("name", "?"),
            "samples": len(samples),
            "avg_temp": round(avg_temp, 1),
            "avg_power_w": round(avg_power, 1),
            "avg_util_pct": round(avg_util, 1),
            "avg_tps": round(avg_tps, 1),
            "efficiency": eff,
            "vram_gb": spec.get("vram_gb", 0),
            "pcie_lanes": spec.get("pcie_lanes", 0),
        }

    async def report(self) -> dict:
        stats = self.snapshot()
        result = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "live": stats,
            "profiles": {idx: self.summarize(idx) for idx in GPU_SPECS},
        }
        if self.redis:
            await self.redis.set(REDIS_KEY, json.dumps(result), ex=60)
        return result


# ── CLI ───────────────────────────────────────────────────────────────────────


async def main():
    import sys

    prof = GPUProfiler()
    await prof.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "live"

    if cmd == "live":
        stats = prof.snapshot()
        print(
            f"{'GPU':<4} {'Name':<12} {'Temp':>5} {'Draw':>7} {'Util':>5} {'MemU':>5} {'Core':>6} {'Mem':>6}"
        )
        print("-" * 58)
        for s in stats:
            print(
                f"{s['idx']:<4} {s['name'][:12]:<12} {s['temp']:>4}°C "
                f"{s['power_draw']:>6.0f}W {s['util_gpu']:>4}% {s['util_mem']:>4}% "
                f"{s['clock_core']:>5}M {s['clock_mem']:>5}M"
            )

    elif cmd == "profile":
        gpu = int(sys.argv[2]) if len(sys.argv) > 2 else 4
        s = prof.summarize(gpu)
        print(json.dumps(s, indent=2))

    elif cmd == "report":
        r = await prof.report()
        for gpu_id, p in r["profiles"].items():
            if "error" not in p:
                print(
                    f"GPU{gpu_id} {p['name']}: "
                    f"avg {p['avg_power_w']}W | {p['avg_temp']}°C | "
                    f"{p['avg_util_pct']}% util | {p['avg_tps']} tok/s"
                )

    elif cmd == "efficiency" and len(sys.argv) >= 4:
        gpu = int(sys.argv[2])
        tps = float(sys.argv[3])
        power = float(sys.argv[4]) if len(sys.argv) > 4 else 200.0
        print(json.dumps(compute_efficiency(gpu, tps, power), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
