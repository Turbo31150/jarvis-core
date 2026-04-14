from enum import Enum
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional

# Enums
class QualityCheck(Enum):
    NULL_RATE = "NULL_RATE"
    DUPLICATE_RATE = "DUPLICATE_RATE"
    SCHEMA_VALID = "SCHEMA_VALID"
    RANGE_VALID = "RANGE_VALID"
    FRESHNESS = "FRESHNESS"
    COMPLETENESS = "COMPLETENESS"

class Severity(Enum):
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"

# Dataclasses
@dataclass
class QualityRule:
    check_type: QualityCheck
    field: str
    params: Dict[str, Any]
    severity: Severity

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
    violations: List[QualityViolation] = field(default_factory=list)
    score: float = 0.0
    passed: bool = True

# Class DataQualityChecker
class DataQualityChecker:
    def __init__(self):
        self.rules = []

    def add_rule(self, rule: QualityRule):
        self.rules.append(rule)

    def check(self, dataset: List[Dict[str, Any]], dataset_name: str) -> QualityReport:
        report = QualityReport(dataset_name=dataset_name, n_records=len(dataset))
        for rule in self.rules:
            violation = None
            if rule.check_type == QualityCheck.NULL_RATE:
                violation = self._check_null_rate(dataset, rule.field, rule.params['threshold'])
            elif rule.check_type == QualityCheck.DUPLICATE_RATE:
                violation = self._check_duplicates(dataset, [rule.field])
            elif rule.check_type == QualityCheck.RANGE_VALID:
                violation = self._check_range(dataset, rule.field, rule.params['min_val'], rule.params['max_val'])
            elif rule.check_type == QualityCheck.FRESHNESS:
                violation = self._check_freshness(dataset, rule.field, rule.params['max_age_hours'])

            if violation:
                report.violations.append(violation)
                report.passed = False

        # Calculate score
        if not report.passed:
            report.score = 1 - (len(report.violations) / len(self.rules))
        else:
            report.score = 1.0

        return report

    def _check_null_rate(self, data: List[Dict[str, Any]], field: str, threshold: float) -> Optional[QualityViolation]:
        null_count = sum(1 for record in data if record.get(field) is None)
        null_rate = null_count / len(data)
        if null_rate > threshold:
            return QualityViolation(
                rule=QualityRule(QualityCheck.NULL_RATE, field, {'threshold': threshold}, Severity.ERROR),
                value=null_rate,
                threshold=threshold,
                message=f"Null rate for {field} is {null_rate:.2%}, above the threshold of {threshold:.2%}"
            )
        return None

    def _check_duplicates(self, data: List[Dict[str, Any]], fields: List[str]) -> Optional[QualityViolation]:
        seen = set()
        duplicates_count = 0
        for record in data:
            key = tuple(record[field] for field in fields)
            if key in seen:
                duplicates_count += 1
            else:
                seen.add(key)
        duplicate_rate = duplicates_count / len(data)
        if duplicate_rate > 0.05:  # Example threshold
            return QualityViolation(
                rule=QualityRule(QualityCheck.DUPLICATE_RATE, ','.join(fields), {}, Severity.ERROR),
                value=duplicate_rate,
                threshold=0.05,
                message=f"Duplicate rate for {','.join(fields)} is {duplicate_rate:.2%}, above the threshold of 5%"
            )
        return None

    def _check_range(self, data: List[Dict[str, Any]], field: str, min_val: float, max_val: float) -> Optional[QualityViolation]:
        out_of_range_count = sum(1 for record in data if not (min_val <= record.get(field, 0) <= max_val))
        out_of_range_rate = out_of_range_count / len(data)
        if out_of_range_rate > 0.05:  # Example threshold
            return QualityViolation(
                rule=QualityRule(QualityCheck.RANGE_VALID, field, {'min_val': min_val, 'max_val': max_val}, Severity.ERROR),
                value=out_of_range_rate,
                threshold=0.05,
                message=f"Out of range rate for {field} is {out_of_range_rate:.2%}, above the threshold of 5%"
            )
        return None

    def _check_freshness(self, data: List[Dict[str, Any]], ts_field: str, max_age_hours: float) -> Optional[QualityViolation]:
        from datetime import datetime, timedelta
        now = datetime.now()
        outdated_count = sum(1 for record in data if (now - datetime.fromisoformat(record.get(ts_field))).total_seconds() / 3600 > max_age_hours)
        outdated_rate = outdated_count / len(data)
        if outdated_rate > 0.05:  # Example threshold
            return QualityViolation(
                rule=QualityRule(QualityCheck.FRESHNESS, ts_field, {'max_age_hours': max_age_hours}, Severity.ERROR),
                value=outdated_rate,
                threshold=0.05,
                message=f"Outdated rate for {ts_field} is {outdated_rate:.2%}, above the threshold of 5%"
            )
        return None

    def profile(self, data: List[Dict[str, Any]]) -> Dict[str, Any]:
        from collections import Counter
        profile = {}
        for record in data:
            for key, value in record.items():
                if key not in profile:
                    profile[key] = []
                profile[key].append(value)
        
        summary = {}
        for key, values in profile.items():
            summary[key] = {
                'count': len(values),
                'unique_count': len(set(values)),
                'min': min(values) if all(isinstance(v, (int, float)) for v in values) else None,
                'max': max(values) if all(isinstance(v, (int, float)) for v in values) else None,
                'mean': sum(values) / len(values) if all(isinstance(v, (int, float)) for v in values) else None
            }
        
        return summary

# Factory function to build JARVIS data quality rules
def build_jarvis_data_quality():
    checker = DataQualityChecker()
    # Example rules
    checker.add_rule(QualityRule(QualityCheck.NULL_RATE, 'age', {'threshold': 0.1}, Severity.ERROR))
    checker.add_rule(QualityRule(QualityCheck.DUPLICATE_RATE, 'email', {}, Severity.ERROR))
    checker.add_rule(QualityRule(QualityCheck.RANGE_VALID, 'salary', {'min_val': 30000, 'max_val': 200000}, Severity.ERROR))
    checker.add_rule(QualityRule(QualityCheck.FRESHNESS, 'created_at', {'max_age_hours': 24}, Severity.ERROR))
    return checker

# Main demo
if __name__ == "__main__":
    # Sample dataset
    data = [
        {"id": 1, "name": "Alice", "age": 30, "email": "alice@example.com", "salary": 50000, "created_at": "2023-10-01T12:00:00"},
        {"id": 2, "name": "Bob", "age": None, "email": "bob@example.com", "salary": 60000, "created_at": "2023-10-02T12:00:00"},
        {"id": 3, "name": "Alice", "age": 30, "email": "alice@example.com", "salary": 50000, "created_at": "2023-10-01T12:00:00"},
        {"id": 4, "name": "Charlie", "age": 40, "email": "charlie@example.com", "salary": 70000, "created_at": "2023-09-01T12:00:00"}
    ]

    # Build JARVIS data quality checker
    jarvis_checker = build_jarvis_data_quality()

    # Check dataset
    report = jarvis_checker.check(data, "Sample Dataset")
    print(f"Dataset Name: {report.dataset_name}")
    print(f"Number of Records: {report.n_records}")
    print(f"Passed: {report.passed}")
    print(f"Score: {report.score:.2f}")
    for violation in report.violations:
        print(f"Violation: {violation.message}")

    # Profile dataset
    profile = jarvis_checker.profile(data)
    print("\nDataset Profile:")
    for key, stats in profile.items():
        print(f"{key}: {stats}")