#!/usr/bin/env python3
"""
jarvis_adaptive_timeout — Dynamic timeout adjustment based on observed latencies
Tracks p95 latency per model/backend, sets timeouts to avoid premature kills
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.adaptive_timeout")

STATS_FILE = Path("/home/turbo/IA/Core/jarvis/data/timeout_stats.json")
REDIS_KEY = "jarvis:adaptive_timeout"
MIN_TIMEOUT = 5.0
MAX_TIMEOUT = 180.0
SAFETY_MARGIN = 1.5  # multiply p95 by this


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 30.0
    sorted_v = sorted(values)
    idx = int(len(sorted_v) * pct / 100)
    return sorted_v[min(idx, len(sorted_v) - 1)]


@dataclass
class TimeoutStats:
    key: str  # f"{backend}:{model}"
    samples: list[float] = field(default_factory=list)
    timeouts: int = 0
    total: int = 0

    @property
    def p50(self) -> float:
        return _percentile(self.samples, 50)

    @property
    def p95(self) -> float:
        return _percentile(self.samples, 95)

    @property
    def p99(self) -> float:
        return _percentile(self.samples, 99)

    @property
    def recommended_timeout(self) -> float:
        if len(self.samples) < 5:
            return 60.0  # default until we have data
        raw = self.p95 * SAFETY_MARGIN
        return round(min(MAX_TIMEOUT, max(MIN_TIMEOUT, raw)), 1)

    def add_sample(self, latency_s: float, timed_out: bool = False):
        self.samples.append(latency_s)
        self.total += 1
        if timed_out:
            self.timeouts += 1
        # Keep last 200 samples
        if len(self.samples) > 200:
            self.samples = self.samples[-200:]

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "samples_count": len(self.samples),
            "p50_s": round(self.p50, 2),
            "p95_s": round(self.p95, 2),
            "p99_s": round(self.p99, 2),
            "recommended_timeout_s": self.recommended_timeout,
            "timeout_rate_pct": round(self.timeouts / max(self.total, 1) * 100, 1),
            "total": self.total,
        }


class AdaptiveTimeout:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._stats: dict[str, TimeoutStats] = {}
        self._load()

    def _load(self):
        if STATS_FILE.exists():
            try:
                data = json.loads(STATS_FILE.read_text())
                for key, d in data.items():
                    s = TimeoutStats(
                        key=key, timeouts=d.get("timeouts", 0), total=d.get("total", 0)
                    )
                    s.samples = d.get("samples", [])
                    self._stats[key] = s
            except Exception:
                pass

    def _save(self):
        STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATS_FILE.write_text(
            json.dumps(
                {
                    k: {
                        "samples": v.samples[-100:],
                        "timeouts": v.timeouts,
                        "total": v.total,
                    }
                    for k, v in self._stats.items()
                },
                indent=2,
            )
        )

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def _key(self, backend: str, model: str) -> str:
        return f"{backend}:{model.split('/')[-1]}"

    def record(
        self, backend: str, model: str, latency_s: float, timed_out: bool = False
    ):
        key = self._key(backend, model)
        if key not in self._stats:
            self._stats[key] = TimeoutStats(key=key)
        self._stats[key].add_sample(latency_s, timed_out)
        self._save()

    def get_timeout(self, backend: str, model: str) -> float:
        key = self._key(backend, model)
        if key not in self._stats:
            return 60.0
        return self._stats[key].recommended_timeout

    async def get_timeout_async(self, backend: str, model: str) -> float:
        """Check Redis first for cluster-shared stats."""
        if self.redis:
            raw = await self.redis.hget(REDIS_KEY, self._key(backend, model))
            if raw:
                try:
                    d = json.loads(raw)
                    return d.get("recommended_timeout_s", 60.0)
                except Exception:
                    pass
        return self.get_timeout(backend, model)

    async def sync_redis(self):
        if not self.redis:
            return
        pipe = self.redis.pipeline()
        for key, stats in self._stats.items():
            pipe.hset(REDIS_KEY, key, json.dumps(stats.to_dict()))
        await pipe.execute()
        await self.redis.expire(REDIS_KEY, 86400)

    def report(self) -> list[dict]:
        return sorted(
            [s.to_dict() for s in self._stats.values()],
            key=lambda x: -x["p95_s"],
        )


async def main():
    import sys

    at = AdaptiveTimeout()
    await at.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"

    if cmd == "report":
        report = at.report()
        if not report:
            print("No timeout data yet")
        else:
            print(
                f"{'Key':<36} {'P50':>6} {'P95':>6} {'P99':>6} {'Timeout':>9} {'TimeoutRate':>12}"
            )
            print("-" * 75)
            for s in report:
                print(
                    f"{s['key']:<36} {s['p50_s']:>5.1f}s {s['p95_s']:>5.1f}s "
                    f"{s['p99_s']:>5.1f}s {s['recommended_timeout_s']:>8.1f}s "
                    f"{s['timeout_rate_pct']:>11.1f}%"
                )

    elif cmd == "get" and len(sys.argv) > 3:
        timeout = at.get_timeout(sys.argv[2], sys.argv[3])
        print(f"Timeout for {sys.argv[2]}:{sys.argv[3]} → {timeout}s")

    elif cmd == "record" and len(sys.argv) > 4:
        at.record(sys.argv[2], sys.argv[3], float(sys.argv[4]))
        print(f"Recorded {sys.argv[4]}s for {sys.argv[2]}:{sys.argv[3]}")

    elif cmd == "sync":
        await at.sync_redis()
        print("Synced to Redis")


if __name__ == "__main__":
    asyncio.run(main())
