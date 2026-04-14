#!/usr/bin/env python3
"""
jarvis_thermal_governor — GPU thermal management + auto power-limit adjustment
Monitors temps, adjusts PL to keep all GPUs under target temp
"""

import asyncio
import json
import logging
import subprocess
import time
from dataclasses import dataclass

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.thermal_governor")

# ── Per-GPU config ─────────────────────────────────────────────────────────

GPU_CONFIG = {
    0: {"name": "RTX 2060", "pl_min": 125, "pl_max": 184, "temp_target": 72},
    1: {"name": "GTX 1660S", "pl_min": 50, "pl_max": 100, "temp_target": 70},
    2: {"name": "GTX 1660S", "pl_min": 50, "pl_max": 100, "temp_target": 70},
    3: {"name": "GTX 1660S", "pl_min": 50, "pl_max": 100, "temp_target": 70},
    4: {"name": "RTX 3080", "pl_min": 130, "pl_max": 320, "temp_target": 75},
}

TEMP_CRITICAL = 85  # hard limit — throttle immediately
TEMP_WARN = 80
POLL_INTERVAL = 10  # seconds
REDIS_KEY = "jarvis:thermal"


@dataclass
class GPUStat:
    idx: int
    temp: int
    power_draw: float
    power_limit: float
    util: int
    mem_used: int
    mem_total: int


def query_gpus() -> list[GPUStat]:
    r = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=index,temperature.gpu,power.draw,power.limit,"
            "utilization.gpu,memory.used,memory.total",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
    )
    stats = []
    for line in r.stdout.strip().split("\n"):
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split(",")]
        try:
            stats.append(
                GPUStat(
                    idx=int(parts[0]),
                    temp=int(parts[1]),
                    power_draw=float(parts[2]),
                    power_limit=float(parts[3]),
                    util=int(parts[4]),
                    mem_used=int(parts[5]),
                    mem_total=int(parts[6]),
                )
            )
        except (ValueError, IndexError):
            continue
    return stats


def set_power_limit(gpu_idx: int, watts: int) -> bool:
    r = subprocess.run(
        ["nvidia-smi", "-i", str(gpu_idx), f"--power-limit={watts}"],
        capture_output=True,
        text=True,
    )
    return r.returncode == 0


def clamp(val: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, val))


class ThermalGovernor:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self.current_limits: dict[int, int] = {
            g: cfg["pl_max"] for g, cfg in GPU_CONFIG.items()
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def govern(self, stats: list[GPUStat]) -> list[dict]:
        actions = []
        for s in stats:
            cfg = GPU_CONFIG.get(s.idx)
            if not cfg:
                continue

            target = cfg["temp_target"]
            current_pl = int(s.power_limit)

            if s.temp >= TEMP_CRITICAL:
                # Emergency: drop to min
                new_pl = cfg["pl_min"]
                reason = f"CRITICAL {s.temp}°C"
            elif s.temp >= TEMP_WARN:
                # Step down 10W
                new_pl = clamp(current_pl - 10, cfg["pl_min"], cfg["pl_max"])
                reason = f"WARN {s.temp}°C"
            elif s.temp > target + 3:
                # Slightly above target: step down 5W
                new_pl = clamp(current_pl - 5, cfg["pl_min"], cfg["pl_max"])
                reason = f"above target {s.temp}°C > {target}°C"
            elif s.temp < target - 8 and s.util > 60:
                # Cool and loaded: can recover 5W
                new_pl = clamp(current_pl + 5, cfg["pl_min"], cfg["pl_max"])
                reason = f"cool+loaded {s.temp}°C, util={s.util}%"
            else:
                continue

            if new_pl != current_pl:
                ok = set_power_limit(s.idx, new_pl)
                if ok:
                    self.current_limits[s.idx] = new_pl
                actions.append(
                    {
                        "gpu": s.idx,
                        "name": cfg["name"],
                        "temp": s.temp,
                        "pl_old": current_pl,
                        "pl_new": new_pl,
                        "reason": reason,
                        "ok": ok,
                    }
                )
                log.info(
                    f"GPU{s.idx} {cfg['name']}: {current_pl}W→{new_pl}W ({reason})"
                )

        return actions

    async def tick(self) -> dict:
        stats = query_gpus()
        actions = self.govern(stats)

        report = {
            "ts": time.strftime("%H:%M:%S"),
            "gpus": [
                {
                    "idx": s.idx,
                    "name": GPU_CONFIG.get(s.idx, {}).get("name", "?"),
                    "temp": s.temp,
                    "power_draw": s.power_draw,
                    "power_limit": s.power_limit,
                    "util": s.util,
                    "mem_pct": round(s.mem_used / s.mem_total * 100, 1),
                }
                for s in stats
            ],
            "actions": actions,
            "hottest": max((s.temp for s in stats), default=0),
        }

        if self.redis:
            await self.redis.set(REDIS_KEY, json.dumps(report), ex=30)
            if actions:
                await self.redis.publish(
                    "jarvis:events",
                    json.dumps({"event": "thermal_action", "actions": actions}),
                )

        return report

    async def run_loop(self):
        await self.connect_redis()
        log.info("Thermal governor started")
        while True:
            try:
                r = await self.tick()
                if r["hottest"] >= TEMP_WARN:
                    log.warning(f"Hottest GPU: {r['hottest']}°C")
            except Exception as e:
                log.error(f"Governor tick error: {e}")
            await asyncio.sleep(POLL_INTERVAL)


# ── CLI ───────────────────────────────────────────────────────────────────────


async def main():
    import sys

    gov = ThermalGovernor()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "status":
        await gov.connect_redis()
        r = await gov.tick()
        print(
            f"{'GPU':<4} {'Name':<12} {'Temp':>5} {'Draw':>7} {'Limit':>7} {'Util':>5} {'Mem%':>5}"
        )
        print("-" * 50)
        for g in r["gpus"]:
            print(
                f"{g['idx']:<4} {g['name']:<12} {g['temp']:>4}°C "
                f"{g['power_draw']:>6.0f}W {g['power_limit']:>6.0f}W "
                f"{g['util']:>4}% {g['mem_pct']:>4}%"
            )
        if r["actions"]:
            print("\nActions:")
            for a in r["actions"]:
                print(f"  GPU{a['gpu']}: {a['pl_old']}W→{a['pl_new']}W ({a['reason']})")

    elif cmd == "daemon":
        logging.basicConfig(level=logging.INFO)
        await gov.run_loop()

    elif cmd == "set" and len(sys.argv) >= 4:
        gpu = int(sys.argv[2])
        pl = int(sys.argv[3])
        ok = set_power_limit(gpu, pl)
        print(f"GPU{gpu} power limit → {pl}W: {'OK' if ok else 'FAILED'}")


if __name__ == "__main__":
    asyncio.run(main())
