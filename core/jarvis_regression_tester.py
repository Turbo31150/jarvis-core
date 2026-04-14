"""
JARVIS Core Module: Regression Tester
Version: 1.1.0
Role: Automated performance and quality regression detection for JARVIS OMEGA nodes.
"""

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("jarvis.quality.regression_tester")

class RegressionSeverity(Enum):
    OK = "ok"
    WARN = "warn"
    CRITICAL = "critical"

@dataclass
class Baseline:
    name: str
    metrics: Dict[str, float]
    version: str
    captured_at: float = field(default_factory=time.time)
    tags: List[str] = field(default_factory=list)

@dataclass
class RegressionResult:
    metric_name: str
    baseline_value: float
    current_value: float
    delta_pct: float
    regressed: bool
    severity: RegressionSeverity

@dataclass
class RegressionReport:
    baseline_name: str
    results: List[RegressionResult]
    overall_status: RegressionSeverity
    n_regressions: int
    n_improvements: int
    timestamp: float = field(default_factory=time.time)

    def to_json(self) -> str:
        data = asdict(self)
        data['overall_status'] = self.overall_status.value
        for r in data['results']:
            r['severity'] = r['severity'].value
        return json.dumps(data, indent=2)

class RegressionTester:
    """
    Benchmarks current performance against historical baselines.
    Detects latency spikes and quality degradation automatically.
    """

    def __init__(self, storage_path: str = "/tmp/jarvis_regression_baselines.json"):
        self.storage_path = storage_path
        self.baselines: Dict[str, Baseline] = {}
        self.history: List[Dict[str, float]] = []
        self._load()

    def _load(self):
        """Loads baselines from disk."""
        if os.path.exists(self.storage_path):
            try:
                with open(self.storage_path, 'r') as f:
                    data = json.load(f)
                    for name, b_data in data.items():
                        self.baselines[name] = Baseline(**b_data)
            except Exception as e:
                logger.error(f"Failed to load baselines: {e}")

    def persist(self):
        """Saves current baselines to disk."""
        try:
            data = {name: asdict(b) for name, b in self.baselines.items()}
            with open(self.storage_path, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to persist baselines: {e}")

    def capture_baseline(self, name: str, metrics: Dict[str, float], 
                         version: str = "1.0.0", tags: List[str] = None) -> Baseline:
        """Saves current metrics as a reference baseline."""
        baseline = Baseline(name, metrics, version, tags=tags or [])
        self.baselines[name] = baseline
        self.persist()
        logger.info(f"Captured new baseline: {name} (v{version})")
        return baseline

    def compare(self, current_metrics: Dict[str, float], 
                baseline_name: str, thresholds: Dict[str, Dict[str, float]] = None) -> RegressionReport:
        """Compares current metrics against a baseline."""
        if baseline_name not in self.baselines:
            raise ValueError(f"Baseline {baseline_name} not found")
            
        base = self.baselines[baseline_name]
        results = []
        
        # Default thresholds
        # For latency: +10% warn, +25% critical (higher is worse)
        # For score: -5% warn, -10% critical (lower is worse)
        default_thresholds = {
            "latency": {"warn": 10.0, "critical": 25.0, "higher_is_worse": True},
            "score": {"warn": -5.0, "critical": -10.0, "higher_is_worse": False},
            "pass_rate": {"warn": -5.0, "critical": -15.0, "higher_is_worse": False}
        }
        active_thresholds = thresholds or default_thresholds
        
        n_regressions = 0
        n_improvements = 0
        max_severity = RegressionSeverity.OK
        
        for m_name, c_val in current_metrics.items():
            if m_name not in base.metrics: continue
            
            b_val = base.metrics[m_name]
            if b_val == 0: continue
            
            delta_pct = ((c_val - b_val) / b_val) * 100
            
            # Find matching threshold config
            t_key = next((k for k in active_thresholds if k in m_name), "score")
            t_cfg = active_thresholds[t_key]
            
            is_worse = (delta_pct > 0) if t_cfg["higher_is_worse"] else (delta_pct < 0)
            severity = RegressionSeverity.OK
            regressed = False
            
            if is_worse:
                abs_delta = abs(delta_pct)
                if abs_delta >= abs(t_cfg["critical"]):
                    severity = RegressionSeverity.CRITICAL
                    regressed = True
                    n_regressions += 1
                elif abs_delta >= abs(t_cfg["warn"]):
                    severity = RegressionSeverity.WARN
                    regressed = True
                    n_regressions += 1
            else:
                if abs(delta_pct) > 5.0: # Significant improvement
                    n_improvements += 1
                    
            if severity.value == "critical" or (severity.value == "warn" and max_severity == RegressionSeverity.OK):
                max_severity = severity
                
            results.append(RegressionResult(
                metric_name=m_name,
                baseline_value=b_val,
                current_value=c_val,
                delta_pct=round(delta_pct, 2),
                regressed=regressed,
                severity=severity
            ))
            
        return RegressionReport(
            baseline_name=baseline_name,
            results=results,
            overall_status=max_severity,
            n_regressions=n_regressions,
            n_improvements=n_improvements
        )

    def trend(self, metric_name: str, n_last: int = 10) -> List[float]:
        """Returns the trend for a specific metric from history."""
        return [h.get(metric_name, 0.0) for h in self.history[-n_last:]]

def build_jarvis_regression_tester() -> RegressionTester:
    """Factory function."""
    return RegressionTester()

def demo():
    tester = build_jarvis_regression_tester()
    
    # 1. Capture baseline
    base_metrics = {
        "avg_latency_ms": 450.0,
        "avg_score": 0.88,
        "pass_rate": 0.95
    }
    tester.capture_baseline("production_v1", base_metrics, version="1.0.0", tags=["stable"])
    
    # 2. Simulate current metrics (Regression)
    current_bad = {
        "avg_latency_ms": 600.0, # +33% (Critical)
        "avg_score": 0.82,       # -6.8% (Warn)
        "pass_rate": 0.94        # -1% (OK)
    }
    
    report = tester.compare(current_bad, "production_v1")
    
    print(f"\n--- REGRESSION REPORT [{report.overall_status.name}] ---")
    print(f"Baseline: {report.baseline_name}")
    print(f"Regressions: {report.n_regressions} | Improvements: {report.n_improvements}")
    print("-" * 65)
    print(f"{'METRIC':<20} | {'BASE':<8} | {'CURR':<8} | {'DELTA%':<8} | {'STATUS'}")
    
    for r in report.results:
        print(f"{r.metric_name:<20} | {r.baseline_value:<8.2f} | {r.current_value:<8.2f} | {r.delta_pct:<+8.1f} | {r.severity.name}")

if __name__ == "__main__":
    demo()
