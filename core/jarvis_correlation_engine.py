#!/usr/bin/env python3
"""
jarvis_correlation_engine — Cross-metric and cross-event correlation analysis
Pearson, Spearman, lag correlation, and causal spike detection
"""

import json
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

log = logging.getLogger("jarvis.correlation_engine")


class CorrelationType(str, Enum):
    PEARSON = "pearson"
    SPEARMAN = "spearman"
    LAG = "lag"  # cross-correlation at various lags
    EVENT = "event"  # event-to-metric correlation


@dataclass
class CorrelationResult:
    metric_a: str
    metric_b: str
    correlation_type: CorrelationType
    coefficient: float  # -1.0 to 1.0
    p_approx: float = 0.0  # approximate p-value
    lag: int = 0  # for lag correlation
    sample_size: int = 0
    ts: float = field(default_factory=time.time)
    interpretation: str = ""

    def is_significant(self, threshold: float = 0.5) -> bool:
        return abs(self.coefficient) >= threshold

    def strength(self) -> str:
        a = abs(self.coefficient)
        if a >= 0.9:
            return "very_strong"
        elif a >= 0.7:
            return "strong"
        elif a >= 0.5:
            return "moderate"
        elif a >= 0.3:
            return "weak"
        return "negligible"

    def to_dict(self) -> dict:
        return {
            "metric_a": self.metric_a,
            "metric_b": self.metric_b,
            "type": self.correlation_type.value,
            "coefficient": round(self.coefficient, 6),
            "p_approx": round(self.p_approx, 6),
            "lag": self.lag,
            "sample_size": self.sample_size,
            "strength": self.strength(),
            "interpretation": self.interpretation,
            "ts": self.ts,
        }


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: list[float], mean: float | None = None) -> float:
    m = mean if mean is not None else _mean(xs)
    n = len(xs)
    if n < 2:
        return 0.0
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def _pearson(xs: list[float], ys: list[float]) -> float:
    n = min(len(xs), len(ys))
    if n < 3:
        return 0.0
    xs, ys = xs[-n:], ys[-n:]
    mx, my = _mean(xs), _mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
    return num / den if den != 0 else 0.0


def _rank(xs: list[float]) -> list[float]:
    sorted_vals = sorted(enumerate(xs), key=lambda t: t[1])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(sorted_vals):
        j = i
        while j < len(sorted_vals) - 1 and sorted_vals[j + 1][1] == sorted_vals[j][1]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            ranks[sorted_vals[k][0]] = avg_rank
        i = j + 1
    return ranks


def _spearman(xs: list[float], ys: list[float]) -> float:
    n = min(len(xs), len(ys))
    if n < 3:
        return 0.0
    return _pearson(_rank(xs[-n:]), _rank(ys[-n:]))


def _t_to_p_approx(r: float, n: int) -> float:
    """Approximate two-tailed p-value from Pearson r and n."""
    if n <= 2 or abs(r) >= 1.0:
        return 0.0
    t = r * math.sqrt((n - 2) / (1 - r**2))
    # Very rough approximation using normal distribution
    z = abs(t) / math.sqrt(n)
    p = 2 * (1 - min(0.9999, 0.5 * (1 + math.erf(z / math.sqrt(2)))))
    return p


def _lag_correlation(
    xs: list[float], ys: list[float], max_lag: int
) -> list[tuple[int, float]]:
    """Compute cross-correlation for lags -max_lag to +max_lag."""
    results = []
    n = min(len(xs), len(ys))
    for lag in range(-max_lag, max_lag + 1):
        if lag >= 0:
            a, b = xs[: n - lag], ys[lag:n]
        else:
            a, b = xs[-lag:n], ys[: n + lag]
        if len(a) < 3:
            continue
        r = _pearson(a, b)
        results.append((lag, r))
    return results


class CorrelationEngine:
    def __init__(self, series_maxlen: int = 3600):
        self._series: dict[str, deque] = {}
        self._maxlen = series_maxlen
        self._results: list[CorrelationResult] = []
        self._max_results = 10_000
        self._stats: dict[str, int] = {
            "records": 0,
            "correlations_computed": 0,
        }

    def record(self, metric_name: str, value: float):
        if metric_name not in self._series:
            self._series[metric_name] = deque(maxlen=self._maxlen)
        self._series[metric_name].append(value)
        self._stats["records"] += 1

    def record_many(self, values: dict[str, float]):
        for name, val in values.items():
            self.record(name, val)

    def correlate(
        self,
        metric_a: str,
        metric_b: str,
        method: CorrelationType = CorrelationType.PEARSON,
        window: int = 100,
    ) -> CorrelationResult | None:
        xa = list(self._series.get(metric_a, []))[-window:]
        xb = list(self._series.get(metric_b, []))[-window:]
        n = min(len(xa), len(xb))
        if n < 5:
            return None

        self._stats["correlations_computed"] += 1

        if method == CorrelationType.PEARSON:
            r = _pearson(xa[-n:], xb[-n:])
            p = _t_to_p_approx(r, n)
            interp = f"{metric_a} and {metric_b} are {'positively' if r > 0 else 'negatively'} correlated (r={r:.2f})"
            result = CorrelationResult(
                metric_a, metric_b, method, r, p, sample_size=n, interpretation=interp
            )
        elif method == CorrelationType.SPEARMAN:
            r = _spearman(xa[-n:], xb[-n:])
            p = _t_to_p_approx(r, n)
            result = CorrelationResult(
                metric_a,
                metric_b,
                method,
                r,
                p,
                sample_size=n,
                interpretation=f"Rank correlation: {r:.2f}",
            )
        else:
            result = CorrelationResult(metric_a, metric_b, method, 0.0, sample_size=n)

        self._results.append(result)
        if len(self._results) > self._max_results:
            self._results.pop(0)
        return result

    def lag_correlate(
        self,
        metric_a: str,
        metric_b: str,
        max_lag: int = 10,
        window: int = 200,
    ) -> list[CorrelationResult]:
        xa = list(self._series.get(metric_a, []))[-window:]
        xb = list(self._series.get(metric_b, []))[-window:]
        if min(len(xa), len(xb)) < 10:
            return []

        lag_results = _lag_correlation(xa, xb, max_lag)
        results = []
        for lag, r in lag_results:
            n = min(len(xa), len(xb)) - abs(lag)
            p = _t_to_p_approx(r, n)
            interp = f"lag={lag}: {metric_a} {'leads' if lag > 0 else 'lags'} {metric_b} by {abs(lag)} steps"
            results.append(
                CorrelationResult(
                    metric_a,
                    metric_b,
                    CorrelationType.LAG,
                    r,
                    p,
                    lag=lag,
                    sample_size=n,
                    interpretation=interp,
                )
            )

        self._stats["correlations_computed"] += len(results)
        best = max(results, key=lambda r: abs(r.coefficient)) if results else None
        if best:
            self._results.append(best)
        return results

    def correlate_all(
        self,
        method: CorrelationType = CorrelationType.PEARSON,
        window: int = 100,
        min_coefficient: float = 0.5,
    ) -> list[CorrelationResult]:
        """Compute pairwise correlations for all series."""
        names = list(self._series.keys())
        significant = []
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                r = self.correlate(names[i], names[j], method, window)
                if r and abs(r.coefficient) >= min_coefficient:
                    significant.append(r)
        significant.sort(key=lambda r: -abs(r.coefficient))
        return significant

    def find_correlated_with(
        self,
        target: str,
        min_coefficient: float = 0.5,
        window: int = 100,
    ) -> list[CorrelationResult]:
        """Find all metrics correlated with target."""
        results = []
        for name in self._series:
            if name == target:
                continue
            r = self.correlate(target, name, CorrelationType.PEARSON, window)
            if r and abs(r.coefficient) >= min_coefficient:
                results.append(r)
        results.sort(key=lambda r: -abs(r.coefficient))
        return results

    def series_names(self) -> list[str]:
        return list(self._series.keys())

    def stats(self) -> dict:
        return {
            **self._stats,
            "series_tracked": len(self._series),
            "results_stored": len(self._results),
        }


def build_jarvis_correlation_engine() -> CorrelationEngine:
    return CorrelationEngine(series_maxlen=3600)


def main():
    import sys
    import random
    import math as _math

    engine = build_jarvis_correlation_engine()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Correlation engine demo...")
        # Simulate correlated metrics
        for i in range(100):
            t = i / 10
            gpu_temp = 60 + _math.sin(t) * 10 + random.gauss(0, 1)
            vram_pct = 40 + _math.sin(t) * 15 + random.gauss(0, 2)  # correlated
            cpu_pct = 30 + random.gauss(0, 10)  # uncorrelated
            # latency lags gpu_temp by 5 steps
            engine.record("gpu.temperature", gpu_temp)
            engine.record("gpu.vram_pct", vram_pct)
            engine.record("system.cpu_pct", cpu_pct)
            engine.record("inference.latency_ms", gpu_temp * 20 + random.gauss(0, 50))

        print("\nPairwise correlations (min |r|=0.4):")
        for r in engine.correlate_all(min_coefficient=0.4):
            sign = "+" if r.coefficient > 0 else ""
            print(
                f"  {r.metric_a:<25} ↔ {r.metric_b:<25} r={sign}{r.coefficient:.3f} [{r.strength()}]"
            )

        print("\nLag correlation: gpu.temperature → inference.latency_ms")
        lag_results = engine.lag_correlate(
            "gpu.temperature", "inference.latency_ms", max_lag=8
        )
        for r in sorted(lag_results, key=lambda x: -abs(x.coefficient))[:3]:
            print(f"  lag={r.lag:+3d}: r={r.coefficient:.3f}")

        print(f"\nStats: {json.dumps(engine.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(engine.stats(), indent=2))


if __name__ == "__main__":
    main()
