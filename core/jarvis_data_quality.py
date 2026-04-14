"""
JARVIS Core Module: Data Quality
Version: 1.1.0
Role: Data validation and profiling for JARVIS OMEGA training and log pipelines.
"""

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple, Union

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("jarvis.data.quality")

class QualityCheck(Enum):
    NULL_RATE = "null_rate"
    DUPLICATE_RATE = "duplicate_rate"
    SCHEMA_VALID = "schema_valid"
    RANGE_VALID = "range_valid"
    FRESHNESS = "freshness"
    COMPLETENESS = "completeness"
    DISTRIBUTION_SHIFT = "distribution_shift"

class QualitySeverity(Enum):
    INFO = 0
    WARN = 1
    ERROR = 2
    CRITICAL = 3

@dataclass
class QualityRule:
    check_type: QualityCheck
    field: str
    params: Dict[str, Any] = field(default_factory=dict)
    severity: QualitySeverity = QualitySeverity.ERROR

@dataclass
class QualityViolation:
    rule: QualityRule
    value: float
    threshold: float
    message: str

@dataclass
class QualityReport:
    dataset_name: str
    n_records: int
    violations: List[QualityViolation]
    score: float
    passed: bool
    timestamp: float = field(default_factory=time.time)

    def to_json(self) -> str:
        # Custom dict conversion to handle Enums
        data = asdict(self)
        for v in data['violations']:
            v['rule']['check_type'] = v['rule']['check_type'].value
            v['rule']['severity'] = v['rule']['severity'].value
        return json.dumps(data, indent=2)

class DataQualityChecker:
    """
    Validates data quality for input records or log streams.
    Implements various checks like null rates, schema validation, and range checks.
    """

    def __init__(self):
        self.rules: List[QualityRule] = []

    def add_rule(self, rule: QualityRule):
        """Adds a quality rule to the checker."""
        self.rules.append(rule)

    def profile(self, data: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Generates a basic profile of the dataset."""
        if not data: return {"n_records": 0}
        
        fields = data[0].keys()
        profile = {"n_records": len(data), "fields": {}}
        
        for f in fields:
            values = [d.get(f) for d in data]
            nulls = sum(1 for v in values if v is None)
            profile["fields"][f] = {
                "null_count": nulls,
                "null_rate": nulls / len(data)
            }
            # Add numeric stats if applicable
            numerics = [v for v in values if isinstance(v, (int, float))]
            if numerics:
                profile["fields"][f].update({
                    "min": min(numerics),
                    "max": max(numerics),
                    "avg": sum(numerics) / len(numerics)
                })
                
        return profile

    def check(self, data: List[Dict[str, Any]], name: str = "dataset") -> QualityReport:
        """Runs all registered rules against the data."""
        if not data:
            return QualityReport(name, 0, [], 1.0, True)
            
        violations = []
        n = len(data)
        
        for rule in self.rules:
            violation = None
            if rule.check_type == QualityCheck.NULL_RATE:
                violation = self._check_null_rate(data, rule)
            elif rule.check_type == QualityCheck.DUPLICATE_RATE:
                violation = self._check_duplicates(data, rule)
            elif rule.check_type == QualityCheck.RANGE_VALID:
                violation = self._check_range(data, rule)
            elif rule.check_type == QualityCheck.SCHEMA_VALID:
                violation = self._check_schema(data, rule)
            elif rule.check_type == QualityCheck.FRESHNESS:
                violation = self._check_freshness(data, rule)
                
            if violation:
                violations.append(violation)
                
        # Calculate score (weighted by severity)
        total_penalty = sum((v.rule.severity.value + 1) * 0.1 for v in violations)
        score = max(0.0, 1.0 - total_penalty)
        passed = all(v.rule.severity.value < QualitySeverity.ERROR.value for v in violations)
        
        return QualityReport(name, n, violations, round(score, 4), passed)

    def _check_null_rate(self, data: List[Dict], rule: QualityRule) -> Optional[QualityViolation]:
        threshold = rule.params.get("max_rate", 0.05)
        nulls = sum(1 for d in data if d.get(rule.field) is None)
        rate = nulls / len(data)
        if rate > threshold:
            return QualityViolation(rule, rate, threshold, f"Null rate {rate:.2%} exceeds threshold {threshold:.2%}")
        return None

    def _check_duplicates(self, data: List[Dict], rule: QualityRule) -> Optional[QualityViolation]:
        threshold = rule.params.get("max_rate", 0.01)
        values = [str(d.get(rule.field)) for d in data]
        dupes = len(values) - len(set(values))
        rate = dupes / len(data)
        if rate > threshold:
            return QualityViolation(rule, rate, threshold, f"Duplicate rate {rate:.2%} exceeds threshold {threshold:.2%}")
        return None

    def _check_range(self, data: List[Dict], rule: QualityRule) -> Optional[QualityViolation]:
        min_v = rule.params.get("min")
        max_v = rule.params.get("max")
        outliers = 0
        for d in data:
            v = d.get(rule.field)
            if v is not None and isinstance(v, (int, float)):
                if (min_v is not None and v < min_v) or (max_v is not None and v > max_v):
                    outliers += 1
        rate = outliers / len(data)
        if rate > 0:
            return QualityViolation(rule, rate, 0.0, f"Detected {outliers} range outliers in field {rule.field}")
        return None

    def _check_schema(self, data: List[Dict], rule: QualityRule) -> Optional[QualityViolation]:
        expected_type = rule.params.get("type")
        if not expected_type: return None
        
        type_map = {"int": int, "float": float, "str": str, "dict": dict, "list": list}
        target_type = type_map.get(expected_type)
        
        invalid = 0
        for d in data:
            v = d.get(rule.field)
            if v is not None and not isinstance(v, target_type):
                invalid += 1
        
        if invalid > 0:
            return QualityViolation(rule, invalid / len(data), 0.0, f"Detected {invalid} schema type mismatches")
        return None

    def _check_freshness(self, data: List[Dict], rule: QualityRule) -> Optional[QualityViolation]:
        max_age_sec = rule.params.get("max_age", 3600)
        now = time.time()
        latest_ts = 0
        for d in data:
            ts = d.get(rule.field, 0)
            if ts > latest_ts: latest_ts = ts
            
        age = now - latest_ts
        if age > max_age_sec:
            return QualityViolation(rule, age, max_age_sec, f"Data is stale: latest record is {age/60:.1f} minutes old")
        return None

def build_jarvis_data_quality() -> DataQualityChecker:
    """Factory with default rules for JARVIS log data."""
    checker = DataQualityChecker()
    # Log records must have a timestamp and not be too old
    checker.add_rule(QualityRule(QualityCheck.FRESHNESS, "timestamp", {"max_age": 300}, QualitySeverity.WARN))
    # 'request_id' must be unique
    checker.add_rule(QualityRule(QualityCheck.DUPLICATE_RATE, "request_id", {"max_rate": 0.0}, QualitySeverity.CRITICAL))
    # 'latency_ms' must be positive and within reasonable bounds
    checker.add_rule(QualityRule(QualityCheck.RANGE_VALID, "latency_ms", {"min": 0, "max": 30000}, QualitySeverity.ERROR))
    # 'model_id' should not be null
    checker.add_rule(QualityRule(QualityCheck.NULL_RATE, "model_id", {"max_rate": 0.01}, QualitySeverity.ERROR))
    
    return checker

def demo():
    checker = build_jarvis_data_quality()
    
    # 1. Profile data
    sample_data = [
        {"timestamp": time.time(), "request_id": "req-1", "latency_ms": 450, "model_id": "qwen"},
        {"timestamp": time.time() - 10, "request_id": "req-2", "latency_ms": 1200, "model_id": "qwen"},
        {"timestamp": time.time() - 20, "request_id": "req-1", "latency_ms": -5, "model_id": None}, # Dupe ID, Negative latency, Null model
        {"timestamp": time.time() - 600, "request_id": "req-3", "latency_ms": 50000, "model_id": "gemma"} # Stale, High latency
    ]
    
    print("\n--- DATA PROFILE ---")
    print(json.dumps(checker.profile(sample_data), indent=2))
    
    # 2. Run Checks
    report = checker.check(sample_data, "LogStream_Audit")
    
    print(f"\n--- QUALITY REPORT [{ 'PASSED' if report.passed else 'FAILED' }] ---")
    print(f"Dataset: {report.dataset_name}")
    print(f"Quality Score: {report.score:.2f}")
    print("-" * 60)
    for v in report.violations:
        print(f"[{v.rule.severity.name}] {v.rule.check_type.name} on '{v.rule.field}': {v.message}")

if __name__ == "__main__":
    demo()
