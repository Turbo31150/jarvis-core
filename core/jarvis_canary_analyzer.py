#!/usr/bin/env python3
"""
jarvis_canary_analyzer — Canary deployment analysis: compare baseline vs canary metrics
Statistical significance testing, automated go/no-go decisions
"""

import json
import logging
import math
import time
from dataclasses import dataclass, field
from enum import Enum

log = logging.getLogger("jarvis.canary_analyzer")


class CanaryVerdict(str, Enum):
    GO = "go"  # canary is better or equivalent
    NO_GO = "no_go"  # canary is worse
    INCONCLUSIVE = "inconclusive"  # insufficient data
    ABORTED = "aborted"


class MetricDirection(str, Enum):
    LOWER_BETTER = "lower_better"  # latency, error rate
    HIGHER_BETTER = "higher_better"  # throughput, success rate


@dataclass
class MetricSample:
    value: float
    ts: float = field(default_factory=time.time)
    labels: dict[str, str] = field(default_factory=dict)


@dataclass
class CanaryMetricConfig:
    name: str
    direction: MetricDirection
    threshold_pct: float = 10.0  # max allowed degradation %
    min_samples: int = 30
    critical: bool = False  # critical metrics cause immediate NO_GO


@dataclass
class MetricComparison:
    metric_name: str
    direction: MetricDirection
    baseline_mean: float
    canary_mean: float
    baseline_std: float
    canary_std: float
    baseline_n: int
    canary_n: int
    delta_pct: float
    p_value: float
    significant: bool
    verdict: CanaryVerdict
    threshold_pct: float

    def to_dict(self) -> dict:
        return {
            "metric": self.metric_name,
            "direction": self.direction.value,
            "baseline_mean": round(self.baseline_mean, 4),
            "canary_mean": round(self.canary_mean, 4),
            "delta_pct": round(self.delta_pct, 4),
            "p_value": round(self.p_value, 6),
            "significant": self.significant,
            "baseline_n": self.baseline_n,
            "canary_n": self.canary_n,
            "verdict": self.verdict.value,
        }


@dataclass
class CanaryReport:
    canary_id: str
    service: str
    baseline_version: str
    canary_version: str
    verdict: CanaryVerdict
    comparisons: list[MetricComparison] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    analyzed_at: float = 0.0
    traffic_pct: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "canary_id": self.canary_id,
            "service": self.service,
            "baseline_version": self.baseline_version,
            "canary_version": self.canary_version,
            "verdict": self.verdict.value,
            "traffic_pct": self.traffic_pct,
            "comparisons": [c.to_dict() for c in self.comparisons],
            "notes": self.notes,
            "started_at": self.started_at,
            "analyzed_at": self.analyzed_at,
        }


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _welch_t_test(a: list[float], b: list[float]) -> float:
    """Welch's t-test — returns approximate two-tailed p-value."""
    na, nb = len(a), len(b)
    if na < 3 or nb < 3:
        return 1.0
    ma, mb = _mean(a), _mean(b)
    va = (_std(a) ** 2) / na
    vb = (_std(b) ** 2) / nb
    se = math.sqrt(va + vb)
    if se == 0:
        return 1.0
    t = abs(ma - mb) / se
    # Approximate p-value using normal distribution
    z = t
    p = 2 * (1 - min(0.9999, 0.5 * (1 + math.erf(z / math.sqrt(2)))))
    return p


class CanaryAnalyzer:
    def __init__(self):
        self._metric_configs: dict[str, CanaryMetricConfig] = {}
        self._baseline: dict[str, list[MetricSample]] = {}
        self._canary: dict[str, list[MetricSample]] = {}
        self._reports: list[CanaryReport] = []
        self._stats: dict[str, int] = {
            "analyses_run": 0,
            "go": 0,
            "no_go": 0,
            "inconclusive": 0,
        }

    def configure_metric(self, config: CanaryMetricConfig):
        self._metric_configs[config.name] = config

    def record_baseline(self, metric: str, value: float, labels: dict | None = None):
        if metric not in self._baseline:
            self._baseline[metric] = []
        self._baseline[metric].append(MetricSample(value=value, labels=labels or {}))

    def record_canary(self, metric: str, value: float, labels: dict | None = None):
        if metric not in self._canary:
            self._canary[metric] = []
        self._canary[metric].append(MetricSample(value=value, labels=labels or {}))

    def record_batch(self, is_canary: bool, values: dict[str, float]):
        fn = self.record_canary if is_canary else self.record_baseline
        for metric, value in values.items():
            fn(metric, value)

    def _compare_metric(
        self, name: str, cfg: CanaryMetricConfig
    ) -> MetricComparison | None:
        base_samples = [s.value for s in self._baseline.get(name, [])]
        can_samples = [s.value for s in self._canary.get(name, [])]

        if len(base_samples) < cfg.min_samples or len(can_samples) < cfg.min_samples:
            return None

        bm = _mean(base_samples)
        cm = _mean(can_samples)
        bs = _std(base_samples)
        cs = _std(can_samples)

        delta_pct = ((cm - bm) / max(abs(bm), 1e-9)) * 100.0
        p_value = _welch_t_test(base_samples, can_samples)
        significant = p_value < 0.05

        # Verdict
        if cfg.direction == MetricDirection.LOWER_BETTER:
            degraded = delta_pct > cfg.threshold_pct
        else:
            degraded = delta_pct < -cfg.threshold_pct

        if not significant:
            verdict = CanaryVerdict.INCONCLUSIVE
        elif degraded:
            verdict = CanaryVerdict.NO_GO
        else:
            verdict = CanaryVerdict.GO

        return MetricComparison(
            metric_name=name,
            direction=cfg.direction,
            baseline_mean=bm,
            canary_mean=cm,
            baseline_std=bs,
            canary_std=cs,
            baseline_n=len(base_samples),
            canary_n=len(can_samples),
            delta_pct=delta_pct,
            p_value=p_value,
            significant=significant,
            verdict=verdict,
            threshold_pct=cfg.threshold_pct,
        )

    def analyze(
        self,
        service: str,
        canary_id: str,
        baseline_version: str = "current",
        canary_version: str = "canary",
        traffic_pct: float = 0.0,
    ) -> CanaryReport:
        import secrets

        cid = canary_id or secrets.token_hex(6)
        comparisons: list[MetricComparison] = []
        notes: list[str] = []
        overall_verdict = CanaryVerdict.GO

        for metric_name, cfg in self._metric_configs.items():
            comp = self._compare_metric(metric_name, cfg)
            if comp is None:
                base_n = len(self._baseline.get(metric_name, []))
                can_n = len(self._canary.get(metric_name, []))
                notes.append(
                    f"{metric_name}: insufficient data (baseline={base_n}, canary={can_n}, min={cfg.min_samples})"
                )
                if overall_verdict == CanaryVerdict.GO:
                    overall_verdict = CanaryVerdict.INCONCLUSIVE
                continue

            comparisons.append(comp)

            if comp.verdict == CanaryVerdict.NO_GO:
                if cfg.critical:
                    overall_verdict = CanaryVerdict.NO_GO
                    notes.append(
                        f"CRITICAL metric {metric_name!r} degraded by {comp.delta_pct:.1f}%"
                    )
                elif overall_verdict != CanaryVerdict.NO_GO:
                    overall_verdict = CanaryVerdict.NO_GO
                    notes.append(
                        f"Metric {metric_name!r} degraded by {comp.delta_pct:.1f}%"
                    )

        self._stats["analyses_run"] += 1
        self._stats[
            overall_verdict.value
            if overall_verdict.value in self._stats
            else "inconclusive"
        ] += 1

        report = CanaryReport(
            canary_id=cid,
            service=service,
            baseline_version=baseline_version,
            canary_version=canary_version,
            verdict=overall_verdict,
            comparisons=comparisons,
            traffic_pct=traffic_pct,
            notes=notes,
            analyzed_at=time.time(),
        )
        self._reports.append(report)

        icon = {"go": "✅", "no_go": "❌", "inconclusive": "⚠️"}.get(
            overall_verdict.value, "?"
        )
        log.info(f"{icon} Canary {cid!r} service={service!r} → {overall_verdict.value}")

        return report

    def clear_samples(self):
        self._baseline.clear()
        self._canary.clear()

    def recent_reports(self, limit: int = 10) -> list[dict]:
        return [r.to_dict() for r in self._reports[-limit:]]

    def stats(self) -> dict:
        return {
            **self._stats,
            "metrics_configured": len(self._metric_configs),
            "baseline_samples": sum(len(v) for v in self._baseline.values()),
            "canary_samples": sum(len(v) for v in self._canary.values()),
        }


def build_jarvis_canary_analyzer() -> CanaryAnalyzer:
    ca = CanaryAnalyzer()

    ca.configure_metric(
        CanaryMetricConfig(
            "inference.latency_ms",
            MetricDirection.LOWER_BETTER,
            threshold_pct=15.0,
            min_samples=30,
            critical=True,
        )
    )
    ca.configure_metric(
        CanaryMetricConfig(
            "inference.error_rate",
            MetricDirection.LOWER_BETTER,
            threshold_pct=5.0,
            min_samples=20,
            critical=True,
        )
    )
    ca.configure_metric(
        CanaryMetricConfig(
            "inference.throughput_rps",
            MetricDirection.HIGHER_BETTER,
            threshold_pct=10.0,
            min_samples=30,
        )
    )
    ca.configure_metric(
        CanaryMetricConfig(
            "gpu.temperature",
            MetricDirection.LOWER_BETTER,
            threshold_pct=5.0,
            min_samples=20,
        )
    )

    return ca


def main():
    import sys
    import random

    ca = build_jarvis_canary_analyzer()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Canary analyzer demo...")

        rng = random.Random(42)

        # Baseline: 50 samples per metric
        for _ in range(50):
            ca.record_batch(
                False,
                {
                    "inference.latency_ms": rng.gauss(200, 20),
                    "inference.error_rate": rng.gauss(0.02, 0.005),
                    "inference.throughput_rps": rng.gauss(100, 10),
                    "gpu.temperature": rng.gauss(65, 3),
                },
            )

        # Canary A (good): slightly faster
        for _ in range(50):
            ca.record_batch(
                True,
                {
                    "inference.latency_ms": rng.gauss(190, 20),  # 5% faster — OK
                    "inference.error_rate": rng.gauss(0.02, 0.005),
                    "inference.throughput_rps": rng.gauss(105, 10),
                    "gpu.temperature": rng.gauss(65, 3),
                },
            )

        report_a = ca.analyze(
            "inference-gw", "canary-v2.1", "v2.0", "v2.1", traffic_pct=10.0
        )
        print(f"\n  Canary v2.1 → {report_a.verdict.value}")
        for c in report_a.comparisons:
            icon = {"go": "✅", "no_go": "❌", "inconclusive": "⚠️"}.get(
                c.verdict.value, "?"
            )
            print(
                f"    {icon} {c.metric_name:<30} Δ={c.delta_pct:+.1f}% p={c.p_value:.4f}"
            )

        ca.clear_samples()

        # Baseline again
        for _ in range(50):
            ca.record_batch(
                False,
                {
                    "inference.latency_ms": rng.gauss(200, 20),
                    "inference.error_rate": rng.gauss(0.02, 0.005),
                    "inference.throughput_rps": rng.gauss(100, 10),
                    "gpu.temperature": rng.gauss(65, 3),
                },
            )

        # Canary B (bad): 25% latency regression
        for _ in range(50):
            ca.record_batch(
                True,
                {
                    "inference.latency_ms": rng.gauss(250, 20),  # 25% slower — CRITICAL
                    "inference.error_rate": rng.gauss(0.02, 0.005),
                    "inference.throughput_rps": rng.gauss(100, 10),
                    "gpu.temperature": rng.gauss(65, 3),
                },
            )

        report_b = ca.analyze(
            "inference-gw", "canary-v2.2", "v2.0", "v2.2", traffic_pct=5.0
        )
        print(f"\n  Canary v2.2 → {report_b.verdict.value}")
        for c in report_b.comparisons:
            icon = {"go": "✅", "no_go": "❌", "inconclusive": "⚠️"}.get(
                c.verdict.value, "?"
            )
            print(f"    {icon} {c.metric_name:<30} Δ={c.delta_pct:+.1f}%")
        for note in report_b.notes:
            print(f"    ⚠ {note}")

        print(f"\nStats: {json.dumps(ca.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(ca.stats(), indent=2))


if __name__ == "__main__":
    main()
