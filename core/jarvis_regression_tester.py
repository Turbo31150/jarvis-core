import os
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class Severity(Enum):
    OK = "OK"
    WARN = "WARN"
    CRITICAL = "CRITICAL"


@dataclass
class Baseline:
    name: str
    metrics: Dict[str, float]
    captured_at: float
    version: str
    tags: List[str] = field(default_factory=list)


@dataclass
class RegressionResult:
    metric_name: str
    baseline_value: float
    current_value: float
    delta_pct: float
    regressed: bool
    severity: Severity


@dataclass
class RegressionReport:
    baseline_name: str
    results: List[RegressionResult]
    overall_status: Severity
    n_regressions: int
    n_improvements: int


class RegressionTester:
    def __init__(self, storage_path: str):
        self.storage_path = storage_path
        if not os.path.exists(storage_path):
            os.makedirs(storage_path)

    def capture_baseline(
        self, name: str, metrics: Dict[str, float], version: str, tags: List[str] = None
    ) -> Baseline:
        baseline = Baseline(
            name=name,
            metrics=metrics,
            captured_at=time.time(),
            version=version,
            tags=tags or [],
        )
        self.persist(baseline)
        return baseline

    def load_baseline(self, name: str) -> Optional[Baseline]:
        for filename in os.listdir(self.storage_path):
            if filename.startswith(f"{name}_"):
                with open(os.path.join(self.storage_path, filename), "r") as f:
                    data = json.load(f)
                    return Baseline(**data)
        return None

    def list_baselines(self) -> List[str]:
        baselines = []
        for filename in os.listdir(self.storage_path):
            if filename.endswith(".json"):
                baselines.append(filename.split("_")[0])
        return baselines

    def compare(
        self, current_metrics: Dict[str, float], baseline_name: str, thresholds=None
    ) -> RegressionReport:
        baseline = self.load_baseline(baseline_name)
        if not baseline:
            raise ValueError(f"Baseline {baseline_name} not found")

        thresholds = thresholds or {
            "latency": {"warn": 1.10, "crit": 1.25},
            "score": {"warn": 0.95, "crit": 0.90},
        }

        results = []
        for metric_name, current_value in current_metrics.items():
            baseline_value = baseline.metrics.get(metric_name)
            if baseline_value is None:
                continue

            delta_pct = (current_value - baseline_value) / baseline_value * 100
            regressed = False
            severity = Severity.OK

            if metric_name.startswith("latency"):
                if current_value > baseline_value * thresholds["latency"]["warn"]:
                    severity = Severity.WARN
                if current_value > baseline_value * thresholds["latency"]["crit"]:
                    severity = Severity.CRITICAL
            elif metric_name.startswith("score"):
                if current_value < baseline_value * thresholds["score"]["warn"]:
                    severity = Severity.WARN
                if current_value < baseline_value * thresholds["score"]["crit"]:
                    severity = Severity.CRITICAL

            results.append(
                RegressionResult(
                    metric_name=metric_name,
                    baseline_value=baseline_value,
                    current_value=current_value,
                    delta_pct=delta_pct,
                    regressed=(severity != Severity.OK),
                    severity=severity,
                )
            )

        n_regressions = sum(result.regressed for result in results)
        n_improvements = len(results) - n_regressions

        overall_status = max(
            (result.severity for result in results), default=Severity.OK
        )

        return RegressionReport(
            baseline_name=baseline.name,
            results=results,
            overall_status=overall_status,
            n_regressions=n_regressions,
            n_improvements=n_improvements,
        )

    def auto_detect(
        self, current_metrics: Dict[str, float], window=5
    ) -> RegressionReport:
        baselines = sorted(
            [self.load_baseline(name) for name in self.list_baselines()],
            key=lambda b: b.captured_at,
        )
        if len(baselines) < window:
            raise ValueError("Not enough baselines to perform auto-detection")

        recent_baselines = baselines[-window:]
        baseline_metrics = {
            metric_name: sum(b.metrics.get(metric_name, 0) for b in recent_baselines)
            / window
            for metric_name in current_metrics
        }

        return self.compare(
            current_metrics,
            "auto_detected",
            thresholds={
                "latency": {"warn": 1.10, "crit": 1.25},
                "score": {"warn": 0.95, "crit": 0.90},
            },
        )

    def persist(self, baseline: Baseline):
        filename = f"{baseline.name}_{int(baseline.captured_at)}.json"
        with open(os.path.join(self.storage_path, filename), "w") as f:
            json.dump(baseline.__dict__, f)

    def load(self, path: str) -> List[Baseline]:
        baselines = []
        for filename in os.listdir(path):
            if filename.endswith(".json"):
                with open(os.path.join(path, filename), "r") as f:
                    data = json.load(f)
                    baselines.append(Baseline(**data))
        return baselines

    def trend(self, metric_name: str, n: int) -> List[float]:
        baselines = sorted(
            [self.load_baseline(name) for name in self.list_baselines()],
            key=lambda b: b.captured_at,
        )
        if len(baselines) < n:
            raise ValueError("Not enough baselines to determine trend")

        return [b.metrics.get(metric_name, 0) for b in baselines[-n:]]


def build_jarvis_regression_tester(storage_path: str = "/tmp/jarvis_regression_baselines.json") -> RegressionTester:
    return RegressionTester(storage_path)


if __name__ == "__main__":
    import time

    # Initialize the regression tester
    rt = build_jarvis_regression_tester("baselines")

    # Capture 3 baselines
    baseline1 = rt.capture_baseline(
        name="baseline1", metrics={"latency": 0.1, "score": 0.9}, version="v1"
    )

    time.sleep(1)

    baseline2 = rt.capture_baseline(
        name="baseline2", metrics={"latency": 0.15, "score": 0.85}, version="v2"
    )

    time.sleep(1)

    baseline3 = rt.capture_baseline(
        name="baseline3", metrics={"latency": 0.2, "score": 0.8}, version="v3"
    )

    # Compare current metrics with the latest baseline
    current_metrics = {"latency": 0.25, "score": 0.75}
    report = rt.compare(current_metrics, "baseline3")

    print(f"Overall Status: {report.overall_status}")
    print(f"Number of Regressions: {report.n_regressions}")
    print(f"Number of Improvements: {report.n_improvements}")

    for result in report.results:
        print(
            f"Metric: {result.metric_name}, Baseline Value: {result.baseline_value}, "
            f"Current Value: {result.current_value}, Delta %: {result.delta_pct:.2f}, "
            f"Regressed: {result.regressed}, Severity: {result.severity}"
        )

    # Auto-detect regression
    auto_report = rt.auto_detect(current_metrics)
    print("\nAuto-Detection Report:")
    print(f"Overall Status: {auto_report.overall_status}")
    print(f"Number of Regressions: {auto_report.n_regressions}")
    print(f"Number of Improvements: {auto_report.n_improvements}")

    for result in auto_report.results:
        print(
            f"Metric: {result.metric_name}, Baseline Value: {result.baseline_value}, "
            f"Current Value: {result.current_value}, Delta %: {result.delta_pct:.2f}, "
            f"Regressed: {result.regressed}, Severity: {result.severity}"
        )

    # Load all baselines
    all_baselines = rt.load("baselines")
    for baseline in all_baselines:
        print(f"Loaded Baseline: {baseline.name}, Version: {baseline.version}")
