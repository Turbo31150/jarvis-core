#!/usr/bin/env python3
"""
jarvis_ab_test_runner — A/B testing framework for LLM prompts, models, and parameters
Tracks experiments, assigns variants, collects metrics, computes statistical significance
"""

import asyncio
import hashlib
import json
import logging
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.ab_test_runner")

REDIS_PREFIX = "jarvis:abtest:"
RESULTS_FILE = Path("/home/turbo/IA/Core/jarvis/data/ab_results.jsonl")


class ExperimentStatus(str, Enum):
    DRAFT = "draft"
    RUNNING = "running"
    PAUSED = "paused"
    CONCLUDED = "concluded"


@dataclass
class Variant:
    name: str
    weight: float = 0.5  # allocation fraction (must sum to 1.0 across variants)
    config: dict = field(default_factory=dict)
    description: str = ""


@dataclass
class Experiment:
    exp_id: str
    name: str
    variants: list[Variant]
    metric_names: list[str]
    status: ExperimentStatus = ExperimentStatus.DRAFT
    min_samples: int = 100
    created_at: float = field(default_factory=time.time)
    concluded_at: float = 0.0
    winner: str = ""

    def get_variant(self, unit_id: str) -> Variant:
        """Deterministic assignment via hash."""
        h = int(hashlib.md5(f"{self.exp_id}:{unit_id}".encode()).hexdigest()[:8], 16)
        bucket = (h % 10000) / 10000.0
        cumulative = 0.0
        for v in self.variants:
            cumulative += v.weight
            if bucket < cumulative:
                return v
        return self.variants[-1]

    def to_dict(self) -> dict:
        return {
            "exp_id": self.exp_id,
            "name": self.name,
            "status": self.status.value,
            "variants": [{"name": v.name, "weight": v.weight} for v in self.variants],
            "min_samples": self.min_samples,
            "winner": self.winner,
        }


@dataclass
class MetricRecord:
    exp_id: str
    variant_name: str
    unit_id: str
    metric_name: str
    value: float
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "exp_id": self.exp_id,
            "variant": self.variant_name,
            "unit_id": self.unit_id,
            "metric": self.metric_name,
            "value": self.value,
            "ts": self.ts,
        }


@dataclass
class VariantStats:
    name: str
    n: int
    mean: float
    std: float
    min_val: float
    max_val: float

    def to_dict(self) -> dict:
        return {
            "variant": self.name,
            "n": self.n,
            "mean": round(self.mean, 4),
            "std": round(self.std, 4),
            "min": round(self.min_val, 4),
            "max": round(self.max_val, 4),
        }


@dataclass
class ExperimentReport:
    exp_id: str
    metric_name: str
    variant_stats: list[VariantStats]
    p_value: float
    significant: bool
    recommended_winner: str
    total_samples: int

    def to_dict(self) -> dict:
        return {
            "exp_id": self.exp_id,
            "metric": self.metric_name,
            "variants": [v.to_dict() for v in self.variant_stats],
            "p_value": round(self.p_value, 4),
            "significant": self.significant,
            "recommended_winner": self.recommended_winner,
            "total_samples": self.total_samples,
        }


def _welch_t_test(a: list[float], b: list[float]) -> float:
    """Two-sample Welch's t-test. Returns approximate p-value."""
    if len(a) < 2 or len(b) < 2:
        return 1.0
    na, nb = len(a), len(b)
    ma = sum(a) / na
    mb = sum(b) / nb
    va = sum((x - ma) ** 2 for x in a) / (na - 1)
    vb = sum((x - mb) ** 2 for x in b) / (nb - 1)
    se = math.sqrt(va / na + vb / nb)
    if se < 1e-12:
        return 1.0
    t = abs(ma - mb) / se
    # Approximate p-value via normal distribution (large sample approximation)
    # P(|Z| > t) ≈ 2 * (1 - Φ(t))
    p = 2 * (1 - _normal_cdf(t))
    return max(0.0, min(1.0, p))


def _normal_cdf(z: float) -> float:
    """Approximation of standard normal CDF."""
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


class ABTestRunner:
    def __init__(self, significance_level: float = 0.05):
        self.redis: aioredis.Redis | None = None
        self._experiments: dict[str, Experiment] = {}
        self._records: list[MetricRecord] = []
        self.significance_level = significance_level
        self._stats: dict[str, int] = {
            "experiments": 0,
            "assignments": 0,
            "records": 0,
            "conclusions": 0,
        }
        RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def create_experiment(self, exp: Experiment) -> Experiment:
        self._experiments[exp.exp_id] = exp
        self._stats["experiments"] += 1
        log.info(f"Experiment created: {exp.exp_id} ({len(exp.variants)} variants)")
        return exp

    def start(self, exp_id: str):
        exp = self._experiments.get(exp_id)
        if exp:
            exp.status = ExperimentStatus.RUNNING

    def pause(self, exp_id: str):
        exp = self._experiments.get(exp_id)
        if exp:
            exp.status = ExperimentStatus.PAUSED

    def assign(self, exp_id: str, unit_id: str) -> Variant | None:
        exp = self._experiments.get(exp_id)
        if not exp or exp.status != ExperimentStatus.RUNNING:
            return None
        self._stats["assignments"] += 1
        return exp.get_variant(unit_id)

    def record(
        self,
        exp_id: str,
        unit_id: str,
        metric_name: str,
        value: float,
    ):
        exp = self._experiments.get(exp_id)
        if not exp:
            return
        variant = exp.get_variant(unit_id)
        rec = MetricRecord(
            exp_id=exp_id,
            variant_name=variant.name,
            unit_id=unit_id,
            metric_name=metric_name,
            value=value,
        )
        self._records.append(rec)
        self._stats["records"] += 1
        # Persist
        with open(RESULTS_FILE, "a") as f:
            f.write(json.dumps(rec.to_dict()) + "\n")

        if self.redis:
            asyncio.create_task(
                self.redis.hincrby(
                    f"{REDIS_PREFIX}{exp_id}:{variant.name}", metric_name, 1
                )
            )

    def _get_values(
        self, exp_id: str, variant_name: str, metric_name: str
    ) -> list[float]:
        return [
            r.value
            for r in self._records
            if r.exp_id == exp_id
            and r.variant_name == variant_name
            and r.metric_name == metric_name
        ]

    def _variant_stats(self, values: list[float], name: str) -> VariantStats:
        n = len(values)
        if n == 0:
            return VariantStats(name, 0, 0.0, 0.0, 0.0, 0.0)
        mean = sum(values) / n
        std = math.sqrt(sum((x - mean) ** 2 for x in values) / max(n - 1, 1))
        return VariantStats(name, n, mean, std, min(values), max(values))

    def analyze(self, exp_id: str, metric_name: str) -> ExperimentReport | None:
        exp = self._experiments.get(exp_id)
        if not exp:
            return None

        all_stats = []
        all_values = {}
        for v in exp.variants:
            vals = self._get_values(exp_id, v.name, metric_name)
            all_values[v.name] = vals
            all_stats.append(self._variant_stats(vals, v.name))

        # Pairwise Welch t-test for 2-variant case
        p_value = 1.0
        if len(exp.variants) == 2:
            a = all_values[exp.variants[0].name]
            b = all_values[exp.variants[1].name]
            p_value = _welch_t_test(a, b)

        significant = p_value < self.significance_level
        recommended = max(all_stats, key=lambda s: s.mean).name if all_stats else ""

        return ExperimentReport(
            exp_id=exp_id,
            metric_name=metric_name,
            variant_stats=all_stats,
            p_value=p_value,
            significant=significant,
            recommended_winner=recommended,
            total_samples=sum(s.n for s in all_stats),
        )

    def conclude(self, exp_id: str, metric_name: str) -> ExperimentReport | None:
        report = self.analyze(exp_id, metric_name)
        if not report:
            return None
        exp = self._experiments[exp_id]
        exp.status = ExperimentStatus.CONCLUDED
        exp.concluded_at = time.time()
        if report.significant:
            exp.winner = report.recommended_winner
        self._stats["conclusions"] += 1
        return report

    def list_experiments(self) -> list[dict]:
        return [e.to_dict() for e in self._experiments.values()]

    def stats(self) -> dict:
        return {**self._stats, "total_records": len(self._records)}


def build_jarvis_ab_runner() -> ABTestRunner:
    runner = ABTestRunner()
    runner.create_experiment(
        Experiment(
            exp_id="prompt_style_v1",
            name="Prompt Style: Concise vs Detailed",
            variants=[
                Variant(
                    "concise",
                    0.5,
                    {"system": "Be extremely concise."},
                    "Short system prompt",
                ),
                Variant(
                    "detailed",
                    0.5,
                    {"system": "You are a helpful assistant. Think step by step."},
                    "Long system prompt",
                ),
            ],
            metric_names=["quality_score", "latency_ms", "user_rating"],
            min_samples=200,
        )
    )
    runner.start("prompt_style_v1")
    return runner


async def main():
    import sys
    import random

    runner = build_jarvis_ab_runner()
    await runner.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        random.seed(42)
        # Simulate 300 users
        for i in range(300):
            unit_id = f"user_{i}"
            variant = runner.assign("prompt_style_v1", unit_id)
            if variant:
                # concise variant has lower latency but slightly worse quality
                if variant.name == "concise":
                    runner.record(
                        "prompt_style_v1",
                        unit_id,
                        "quality_score",
                        random.gauss(0.72, 0.1),
                    )
                    runner.record(
                        "prompt_style_v1", unit_id, "latency_ms", random.gauss(450, 80)
                    )
                else:
                    runner.record(
                        "prompt_style_v1",
                        unit_id,
                        "quality_score",
                        random.gauss(0.81, 0.1),
                    )
                    runner.record(
                        "prompt_style_v1", unit_id, "latency_ms", random.gauss(720, 120)
                    )

        for metric in ["quality_score", "latency_ms"]:
            report = runner.analyze("prompt_style_v1", metric)
            if report:
                print(f"\nMetric: {metric}")
                for vs in report.variant_stats:
                    print(
                        f"  {vs.name:<12} n={vs.n} mean={vs.mean:.3f} std={vs.std:.3f}"
                    )
                sig = "✅ SIGNIFICANT" if report.significant else "❌ not significant"
                print(
                    f"  p={report.p_value:.4f} {sig} → winner: {report.recommended_winner}"
                )

        print(f"\nStats: {json.dumps(runner.stats(), indent=2)}")

    elif cmd == "list":
        for e in runner.list_experiments():
            print(f"  {e['exp_id']:<25} status={e['status']}")

    elif cmd == "stats":
        print(json.dumps(runner.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
