#!/usr/bin/env python3
"""
jarvis_power_monitor — Continuous GPU power tracking, perf/watt scoring
Records time-series, detects regressions, suggests optimal PL
"""

import asyncio
import json
import logging
import sqlite3
import subprocess
import time
from pathlib import Path

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.power_monitor")

DB_PATH = Path("/home/turbo/IA/Core/jarvis/data/power_monitor.db")
REDIS_KEY = "jarvis:power"
POLL_INTERVAL = 15  # seconds


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS gpu_power (
            ts          INTEGER NOT NULL,
            gpu_idx     INTEGER NOT NULL,
            temp        INTEGER,
            power_draw  REAL,
            power_limit REAL,
            util        INTEGER,
            mem_used    INTEGER,
            tps         REAL DEFAULT 0
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_ts ON gpu_power(ts)")
    con.commit()
    con.close()


def query_gpus() -> list[dict]:
    r = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=index,temperature.gpu,power.draw,power.limit,"
            "utilization.gpu,memory.used",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
    )
    out = []
    for line in r.stdout.strip().split("\n"):
        if not line.strip():
            continue
        p = [x.strip() for x in line.split(",")]
        try:
            out.append(
                {
                    "idx": int(p[0]),
                    "temp": int(p[1]),
                    "power_draw": float(p[2]),
                    "power_limit": float(p[3]),
                    "util": int(p[4]),
                    "mem_used": int(p[5]),
                }
            )
        except (ValueError, IndexError):
            continue
    return out


def save_sample(stats: list[dict], tps_map: dict[int, float] | None = None):
    tps_map = tps_map or {}
    con = sqlite3.connect(DB_PATH)
    ts = int(time.time())
    con.executemany(
        "INSERT INTO gpu_power VALUES (?,?,?,?,?,?,?,?)",
        [
            (
                ts,
                s["idx"],
                s["temp"],
                s["power_draw"],
                s["power_limit"],
                s["util"],
                s["mem_used"],
                tps_map.get(s["idx"], 0.0),
            )
            for s in stats
        ],
    )
    con.commit()
    con.close()


def compute_perf_watt(gpu_idx: int, window_min: int = 10) -> dict:
    """Compute perf/watt score for a GPU over last N minutes."""
    con = sqlite3.connect(DB_PATH)
    since = int(time.time()) - window_min * 60
    rows = con.execute(
        "SELECT power_draw, tps, temp FROM gpu_power "
        "WHERE gpu_idx=? AND ts>? AND power_draw>0 ORDER BY ts",
        (gpu_idx, since),
    ).fetchall()
    con.close()

    if not rows:
        return {"gpu": gpu_idx, "error": "no data"}

    avg_power = sum(r[0] for r in rows) / len(rows)
    avg_tps = sum(r[1] for r in rows) / len(rows)
    avg_temp = sum(r[2] for r in rows) / len(rows)
    score = avg_tps / avg_power if avg_power > 0 else 0

    return {
        "gpu": gpu_idx,
        "samples": len(rows),
        "avg_power_w": round(avg_power, 1),
        "avg_tps": round(avg_tps, 2),
        "avg_temp": round(avg_temp, 1),
        "perf_watt": round(score, 4),
    }


def suggest_pl(gpu_idx: int) -> dict:
    """
    Analyze power samples and suggest optimal PL.
    Strategy: find PL bracket where perf/watt is highest.
    """
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT power_limit, AVG(tps), AVG(power_draw), COUNT(*) "
        "FROM gpu_power WHERE gpu_idx=? AND tps>0 "
        "GROUP BY ROUND(power_limit/10)*10 ORDER BY power_limit",
        (gpu_idx,),
    ).fetchall()
    con.close()

    if not rows:
        return {"gpu": gpu_idx, "error": "insufficient data"}

    best = max(rows, key=lambda r: (r[1] / r[2]) if r[2] > 0 else 0)
    brackets = [
        {
            "pl": round(r[0], 0),
            "avg_tps": round(r[1], 2),
            "avg_power": round(r[2], 1),
            "perf_watt": round(r[1] / r[2], 4) if r[2] > 0 else 0,
            "samples": r[3],
        }
        for r in rows
    ]

    return {
        "gpu": gpu_idx,
        "suggested_pl": round(best[0], 0),
        "brackets": brackets,
    }


def total_power_summary(minutes: int = 60) -> dict:
    """Cluster-wide power summary."""
    con = sqlite3.connect(DB_PATH)
    since = int(time.time()) - minutes * 60
    rows = con.execute(
        "SELECT gpu_idx, AVG(power_draw), MAX(temp), AVG(util) "
        "FROM gpu_power WHERE ts>? GROUP BY gpu_idx ORDER BY gpu_idx",
        (since,),
    ).fetchall()
    con.close()

    gpus = [
        {
            "gpu": r[0],
            "avg_power": round(r[1], 1),
            "max_temp": r[2],
            "avg_util": round(r[3], 1),
        }
        for r in rows
    ]
    total = sum(g["avg_power"] for g in gpus)
    return {"window_min": minutes, "total_w": round(total, 1), "gpus": gpus}


class PowerMonitor:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        init_db()

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    async def tick(self) -> dict:
        stats = query_gpus()
        save_sample(stats)

        report = {
            "ts": time.strftime("%H:%M:%S"),
            "gpus": stats,
            "total_w": round(sum(s["power_draw"] for s in stats), 1),
        }

        if self.redis:
            await self.redis.set(REDIS_KEY, json.dumps(report), ex=30)

        return report

    async def run_loop(self):
        await self.connect_redis()
        log.info("Power monitor started")
        while True:
            try:
                await self.tick()
            except Exception as e:
                log.error(f"Power monitor error: {e}")
            await asyncio.sleep(POLL_INTERVAL)


# ── CLI ───────────────────────────────────────────────────────────────────────


async def main():
    import sys

    mon = PowerMonitor()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "status":
        await mon.connect_redis()
        r = await mon.tick()
        print(f"Total draw: {r['total_w']}W")
        print(f"{'GPU':>4} {'Temp':>5} {'Draw':>7} {'Limit':>7} {'Util':>5} {'MEM':>6}")
        print("-" * 38)
        for g in r["gpus"]:
            print(
                f"{g['idx']:>4} {g['temp']:>4}°C {g['power_draw']:>6.0f}W "
                f"{g['power_limit']:>6.0f}W {g['util']:>4}% {g['mem_used']:>5}M"
            )

    elif cmd == "perf-watt":
        gpu = int(sys.argv[2]) if len(sys.argv) > 2 else 4
        print(json.dumps(compute_perf_watt(gpu), indent=2))

    elif cmd == "suggest":
        gpu = int(sys.argv[2]) if len(sys.argv) > 2 else 4
        print(json.dumps(suggest_pl(gpu), indent=2))

    elif cmd == "summary":
        mins = int(sys.argv[2]) if len(sys.argv) > 2 else 60
        print(json.dumps(total_power_summary(mins), indent=2))

    elif cmd == "daemon":
        logging.basicConfig(level=logging.INFO)
        await mon.run_loop()


if __name__ == "__main__":
    asyncio.run(main())
