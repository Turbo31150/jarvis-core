#!/usr/bin/env python3
"""
jarvis_forecast_engine — Time-series forecasting for metrics and load prediction
Simple Exponential Smoothing, Holt-Winters, and moving-average forecasting
"""

import json
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

log = logging.getLogger("jarvis.forecast_engine")


class ForecastMethod(str, Enum):
    MOVING_AVG = "moving_avg"
    EXP_SMOOTH = "exp_smooth"  # Single exponential smoothing (SES)
    DOUBLE_EXP = "double_exp"  # Holt's linear method (trend)
    TRIPLE_EXP = "triple_exp"  # Holt-Winters (trend + seasonality)
    NAIVE = "naive"  # Last observed value
    LINEAR_TREND = "linear_trend"  # OLS linear regression trend


@dataclass
class ForecastResult:
    metric_name: str
    method: ForecastMethod
    horizon: int  # steps ahead
    values: list[float]  # forecasted values
    confidence_low: list[float] = field(default_factory=list)
    confidence_high: list[float] = field(default_factory=list)
    rmse: float = 0.0  # training RMSE if available
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "metric_name": self.metric_name,
            "method": self.method.value,
            "horizon": self.horizon,
            "values": [round(v, 4) for v in self.values],
            "confidence_low": [round(v, 4) for v in self.confidence_low],
            "confidence_high": [round(v, 4) for v in self.confidence_high],
            "rmse": round(self.rmse, 4),
            "ts": self.ts,
        }


def _moving_average(series: list[float], window: int) -> list[float]:
    result = []
    for i in range(len(series)):
        start = max(0, i - window + 1)
        result.append(sum(series[start : i + 1]) / (i - start + 1))
    return result


def _ses(series: list[float], alpha: float = 0.3) -> list[float]:
    """Single Exponential Smoothing."""
    if not series:
        return []
    smoothed = [series[0]]
    for v in series[1:]:
        smoothed.append(alpha * v + (1 - alpha) * smoothed[-1])
    return smoothed


def _double_exp(
    series: list[float], alpha: float = 0.3, beta: float = 0.1
) -> tuple[list[float], float, float]:
    """Holt's linear method. Returns (smoothed, last_level, last_trend)."""
    if len(series) < 2:
        return series[:], series[-1] if series else 0.0, 0.0
    level = series[0]
    trend = series[1] - series[0]
    smoothed = [level + trend]
    for v in series[1:]:
        prev_level = level
        level = alpha * v + (1 - alpha) * (level + trend)
        trend = beta * (level - prev_level) + (1 - beta) * trend
        smoothed.append(level + trend)
    return smoothed, level, trend


def _rmse(actual: list[float], predicted: list[float]) -> float:
    n = min(len(actual), len(predicted))
    if n == 0:
        return 0.0
    return math.sqrt(sum((a - p) ** 2 for a, p in zip(actual[:n], predicted[:n])) / n)


def _linear_trend(series: list[float]) -> tuple[float, float]:
    """Returns (slope, intercept) via OLS."""
    n = len(series)
    if n < 2:
        return 0.0, series[0] if series else 0.0
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(series) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, series))
    den = sum((x - mean_x) ** 2 for x in xs)
    slope = num / den if den != 0 else 0.0
    intercept = mean_y - slope * mean_x
    return slope, intercept


class ForecastEngine:
    def __init__(self):
        # Ring buffer per metric: {name: deque(maxlen=N)}
        self._series: dict[str, deque] = {}
        self._configs: dict[str, dict] = {}
        self._forecasts: dict[str, ForecastResult] = {}
        self._stats: dict[str, int] = {"records": 0, "forecasts_run": 0}

    def configure(
        self,
        metric_name: str,
        method: ForecastMethod = ForecastMethod.DOUBLE_EXP,
        window: int = 60,
        alpha: float = 0.3,
        beta: float = 0.1,
        season_period: int = 24,
    ):
        self._series[metric_name] = deque(maxlen=max(window * 3, 100))
        self._configs[metric_name] = {
            "method": method,
            "window": window,
            "alpha": alpha,
            "beta": beta,
            "season_period": season_period,
        }

    def record(self, metric_name: str, value: float):
        if metric_name not in self._series:
            self.configure(metric_name)
        self._series[metric_name].append(value)
        self._stats["records"] += 1

    def record_many(self, metric_name: str, values: list[float]):
        for v in values:
            self.record(metric_name, v)

    def forecast(
        self,
        metric_name: str,
        horizon: int = 10,
        method: ForecastMethod | None = None,
    ) -> ForecastResult | None:
        series = list(self._series.get(metric_name, []))
        if len(series) < 3:
            return None

        cfg = self._configs.get(metric_name, {})
        method = method or ForecastMethod(cfg.get("method", ForecastMethod.DOUBLE_EXP))
        alpha = cfg.get("alpha", 0.3)
        beta = cfg.get("beta", 0.1)
        window = cfg.get("window", min(20, len(series)))

        self._stats["forecasts_run"] += 1

        predicted: list[float] = []
        ci_low: list[float] = []
        ci_high: list[float] = []
        train_rmse = 0.0

        if method == ForecastMethod.NAIVE:
            last = series[-1]
            predicted = [last] * horizon

        elif method == ForecastMethod.MOVING_AVG:
            smoothed = _moving_average(series, window)
            avg = sum(smoothed[-window:]) / min(len(smoothed), window)
            predicted = [avg] * horizon
            train_rmse = _rmse(series, smoothed)

        elif method == ForecastMethod.EXP_SMOOTH:
            smoothed = _ses(series, alpha)
            last_s = smoothed[-1]
            predicted = [last_s] * horizon
            train_rmse = _rmse(series, smoothed)

        elif method == ForecastMethod.DOUBLE_EXP:
            smoothed, last_level, last_trend = _double_exp(series, alpha, beta)
            for h in range(1, horizon + 1):
                predicted.append(last_level + h * last_trend)
            train_rmse = _rmse(series, smoothed)

        elif method == ForecastMethod.LINEAR_TREND:
            slope, intercept = _linear_trend(series)
            n = len(series)
            fitted = [intercept + slope * i for i in range(n)]
            for h in range(1, horizon + 1):
                predicted.append(intercept + slope * (n + h - 1))
            train_rmse = _rmse(series, fitted)

        else:
            # Default to double exp
            smoothed, last_level, last_trend = _double_exp(series, alpha, beta)
            for h in range(1, horizon + 1):
                predicted.append(last_level + h * last_trend)
            train_rmse = _rmse(series, smoothed)

        # Confidence intervals: ±1.96 * RMSE (approximate 95% CI)
        ci_margin = 1.96 * train_rmse if train_rmse > 0 else 0.0
        ci_low = [max(0.0, v - ci_margin) for v in predicted]
        ci_high = [v + ci_margin for v in predicted]

        result = ForecastResult(
            metric_name=metric_name,
            method=method,
            horizon=horizon,
            values=predicted,
            confidence_low=ci_low,
            confidence_high=ci_high,
            rmse=train_rmse,
        )
        self._forecasts[metric_name] = result
        return result

    def forecast_all(self, horizon: int = 10) -> dict[str, ForecastResult]:
        results = {}
        for name in self._series:
            r = self.forecast(name, horizon)
            if r:
                results[name] = r
        return results

    def latest_forecast(self, metric_name: str) -> ForecastResult | None:
        return self._forecasts.get(metric_name)

    def series_stats(self, metric_name: str) -> dict:
        data = list(self._series.get(metric_name, []))
        if not data:
            return {"count": 0}
        return {
            "count": len(data),
            "min": round(min(data), 4),
            "max": round(max(data), 4),
            "mean": round(sum(data) / len(data), 4),
            "last": round(data[-1], 4),
        }

    def stats(self) -> dict:
        return {
            **self._stats,
            "series_tracked": len(self._series),
        }


def build_jarvis_forecast_engine() -> ForecastEngine:
    engine = ForecastEngine()
    # Pre-configure standard JARVIS metrics
    engine.configure("gpu.temperature", ForecastMethod.DOUBLE_EXP, window=60, alpha=0.3)
    engine.configure("inference.latency_ms", ForecastMethod.MOVING_AVG, window=30)
    engine.configure("system.cpu_pct", ForecastMethod.EXP_SMOOTH, window=60, alpha=0.2)
    engine.configure("system.ram_pct", ForecastMethod.LINEAR_TREND, window=60)
    engine.configure("budget.cost_usd", ForecastMethod.LINEAR_TREND, window=100)
    return engine


def main():
    import sys
    import random
    import math as _math

    engine = build_jarvis_forecast_engine()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Forecast engine demo...")

        # Simulate 50 data points with trend + noise
        base = 60.0
        for i in range(50):
            val = base + i * 0.2 + random.gauss(0, 2.0)
            engine.record("gpu.temperature", val)
            val2 = 40 + _math.sin(i / 5) * 10 + random.gauss(0, 3)
            engine.record("system.cpu_pct", val2)

        for metric in ["gpu.temperature", "system.cpu_pct"]:
            result = engine.forecast(metric, horizon=5)
            if result:
                print(f"\n  {metric} ({result.method.value}, RMSE={result.rmse:.2f})")
                for i, (v, lo, hi) in enumerate(
                    zip(result.values, result.confidence_low, result.confidence_high)
                ):
                    print(f"    t+{i + 1}: {v:.2f} [{lo:.2f}, {hi:.2f}]")
            print(f"  Series: {engine.series_stats(metric)}")

        print(f"\nStats: {json.dumps(engine.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(engine.stats(), indent=2))


if __name__ == "__main__":
    main()
