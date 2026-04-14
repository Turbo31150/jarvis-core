#!/usr/bin/env python3
"""
jarvis_model_health_tracker — Per-model health scoring and SLO tracking
Tracks latency, error rate, throughput per model with SLO violation detection
"""

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.model_health_tracker")

REDIS_PREFIX = "jarvis:mhealth:"
HEALTH_DB = Path("/home/turbo/IA/Core/jarvis/data/model_health.jsonl")

# Default SLO targets
DEFAULT_SLO = {
    "latency_p95_ms": 10000.0,
    "error_rate_pct": 5.0,
    "availability_pct": 95.0,
    "min_tok_per_s": 5.0,
}


@dataclass
class RequestRecord:
    ts: float
    latency_ms: float
    tok_per_s: float
    success: bool
    tokens: int = 0


@dataclass
class ModelHealthSnapshot:
    model_id: str
    node: str
    window_s: float
    request_count: int
    error_count: int
    latency_p50: float
    latency_p95: float
    latency_p99: float
    avg_tok_per_s: float
    availability_pct: float
    health_score: float  # 0-100
    slo_violations: list[str]

    @property
    def healthy(self) -> bool:
        return self.health_score >= 70.0 and not self.slo_violations

    def to_dict(self) -> dict:
        return {
            "model_id": self.model_id,
            "node": self.node,
            "window_s": self.window_s,
            "request_count": self.request_count,
            "error_rate_pct": round(
                self.error_count / max(self.request_count, 1) * 100, 1
            ),
            "latency_p50": round(self.latency_p50, 1),
            "latency_p95": round(self.latency_p95, 1),
            "latency_p99": round(self.latency_p99, 1),
            "avg_tok_per_s": round(self.avg_tok_per_s, 1),
            "availability_pct": round(self.availability_pct, 1),
            "health_score": round(self.health_score, 1),
            "slo_violations": self.slo_violations,
            "healthy": self.healthy,
        }


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    sorted_v = sorted(values)
    idx = int(len(sorted_v) * pct / 100)
    idx = min(idx, len(sorted_v) - 1)
    return sorted_v[idx]


class ModelHealthTracker:
    def __init__(self, window_s: float = 300.0, max_records: int = 1000):
        self.redis: aioredis.Redis | None = None
        self.window_s = window_s
        self.max_records = max_records
        self._records: dict[str, deque] = {}  # "node:model" → deque[RequestRecord]
        self._slo: dict[str, dict] = {}  # "node:model" → SLO config
        HEALTH_DB.parent.mkdir(parents=True, exist_ok=True)

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def _key(self, model_id: str, node: str) -> str:
        return f"{node}:{model_id}"

    def set_slo(self, model_id: str, node: str, **slo_targets):
        key = self._key(model_id, node)
        self._slo[key] = {**DEFAULT_SLO, **slo_targets}

    def record(
        self,
        model_id: str,
        node: str,
        latency_ms: float,
        success: bool,
        tokens: int = 0,
        tok_per_s: float = 0.0,
    ) -> RequestRecord:
        key = self._key(model_id, node)
        if key not in self._records:
            self._records[key] = deque(maxlen=self.max_records)

        rec = RequestRecord(
            ts=time.time(),
            latency_ms=latency_ms,
            tok_per_s=tok_per_s,
            success=success,
            tokens=tokens,
        )
        self._records[key].append(rec)

        if self.redis:
            asyncio.create_task(self._push_redis(model_id, node, rec))

        return rec

    async def _push_redis(self, model_id: str, node: str, rec: RequestRecord):
        key = self._key(model_id, node)
        await self.redis.lpush(
            f"{REDIS_PREFIX}{key}",
            json.dumps({"ts": rec.ts, "latency_ms": rec.latency_ms, "ok": rec.success}),
        )
        await self.redis.ltrim(f"{REDIS_PREFIX}{key}", 0, self.max_records - 1)
        await self.redis.expire(f"{REDIS_PREFIX}{key}", 86400)

    def snapshot(
        self, model_id: str, node: str, window_s: float | None = None
    ) -> ModelHealthSnapshot:
        key = self._key(model_id, node)
        records = list(self._records.get(key, []))
        win = window_s or self.window_s
        cutoff = time.time() - win
        recent = [r for r in records if r.ts >= cutoff]

        if not recent:
            return ModelHealthSnapshot(
                model_id=model_id,
                node=node,
                window_s=win,
                request_count=0,
                error_count=0,
                latency_p50=0,
                latency_p95=0,
                latency_p99=0,
                avg_tok_per_s=0,
                availability_pct=100.0,
                health_score=100.0,
                slo_violations=[],
            )

        latencies = [r.latency_ms for r in recent]
        error_count = sum(1 for r in recent if not r.success)
        error_rate = error_count / len(recent) * 100
        tok_rates = [r.tok_per_s for r in recent if r.tok_per_s > 0]
        availability = (len(recent) - error_count) / len(recent) * 100

        p50 = _percentile(latencies, 50)
        p95 = _percentile(latencies, 95)
        p99 = _percentile(latencies, 99)
        avg_tps = sum(tok_rates) / max(len(tok_rates), 1)

        # SLO violation check
        slo = self._slo.get(key, DEFAULT_SLO)
        violations = []
        if p95 > slo["latency_p95_ms"]:
            violations.append(
                f"latency_p95={p95:.0f}ms > {slo['latency_p95_ms']:.0f}ms"
            )
        if error_rate > slo["error_rate_pct"]:
            violations.append(
                f"error_rate={error_rate:.1f}% > {slo['error_rate_pct']}%"
            )
        if availability < slo["availability_pct"]:
            violations.append(
                f"availability={availability:.1f}% < {slo['availability_pct']}%"
            )
        if tok_rates and avg_tps < slo["min_tok_per_s"]:
            violations.append(f"tok_per_s={avg_tps:.1f} < {slo['min_tok_per_s']}")

        # Health score (0-100)
        health = 100.0
        # Penalize for latency
        latency_budget = slo["latency_p95_ms"]
        if p95 > 0:
            health -= min(30, max(0, (p95 / latency_budget - 1) * 30))
        # Penalize for errors
        health -= min(40, error_rate * 4)
        # Penalize for SLO violations
        health -= len(violations) * 10
        health = max(0.0, min(100.0, health))

        return ModelHealthSnapshot(
            model_id=model_id,
            node=node,
            window_s=win,
            request_count=len(recent),
            error_count=error_count,
            latency_p50=p50,
            latency_p95=p95,
            latency_p99=p99,
            avg_tok_per_s=avg_tps,
            availability_pct=availability,
            health_score=health,
            slo_violations=violations,
        )

    def all_snapshots(self, window_s: float | None = None) -> list[ModelHealthSnapshot]:
        snaps = []
        for key in self._records:
            node, model_id = key.split(":", 1)
            snaps.append(self.snapshot(model_id, node, window_s))
        return sorted(snaps, key=lambda s: s.health_score)

    def top_performers(self, n: int = 3) -> list[ModelHealthSnapshot]:
        return sorted(self.all_snapshots(), key=lambda s: s.health_score, reverse=True)[
            :n
        ]

    def degraded(self) -> list[ModelHealthSnapshot]:
        return [s for s in self.all_snapshots() if not s.healthy]

    def stats(self) -> dict:
        snaps = self.all_snapshots()
        return {
            "models_tracked": len(snaps),
            "healthy": sum(1 for s in snaps if s.healthy),
            "degraded": sum(1 for s in snaps if not s.healthy),
            "total_requests": sum(s.request_count for s in snaps),
            "avg_health_score": round(
                sum(s.health_score for s in snaps) / max(len(snaps), 1), 1
            ),
        }


async def main():
    import sys
    import random

    tracker = ModelHealthTracker(window_s=120.0)
    await tracker.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        models = [
            ("qwen/qwen3.5-9b", "M1"),
            ("qwen/qwen3.5-27b-claude", "M1"),
            ("deepseek-r1-0528", "M2"),
        ]

        # Simulate requests
        for model_id, node in models:
            tracker.set_slo(model_id, node, latency_p95_ms=5000.0)
            for _ in range(50):
                latency = random.gauss(500 if "9b" in model_id else 2000, 200)
                success = random.random() > (0.02 if "9b" in model_id else 0.08)
                tps = random.gauss(40, 5) if "9b" in model_id else random.gauss(15, 3)
                tracker.record(
                    model_id, node, max(50, latency), success, tok_per_s=max(1, tps)
                )

        print(
            f"{'Model':<35} {'Node':<5} {'Score':>6} {'P95':>8} {'Err%':>6} {'TPS':>6} {'SLO Violations'}"
        )
        print("-" * 90)
        for snap in sorted(
            tracker.all_snapshots(), key=lambda s: s.health_score, reverse=True
        ):
            viols = ", ".join(snap.slo_violations) or "none"
            icon = "✅" if snap.healthy else "⚠️"
            print(
                f"  {icon} {snap.model_id:<33} {snap.node:<5} {snap.health_score:>5.1f} {snap.latency_p95:>7.0f}ms {snap.error_count / max(snap.request_count, 1) * 100:>5.1f}% {snap.avg_tok_per_s:>5.1f} {viols}"
            )

        print(f"\nStats: {json.dumps(tracker.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(tracker.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
