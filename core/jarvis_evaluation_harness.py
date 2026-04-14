"""
JARVIS Core Module: Evaluation Harness
Version: 1.1.0
Role: Automated performance and accuracy evaluation for JARVIS OMEGA LLM nodes.
"""

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("jarvis.quality.eval_harness")

@dataclass
class EvalCase:
    id: str
    prompt: str
    expected_output: str
    tags: List[str] = field(default_factory=list)
    weight: float = 1.0
    metadata: Dict[str, Any] = field(default_factory=dict)

@dataclass
class EvalResult:
    case_id: str
    actual_output: str
    score: float
    passed: bool
    latency_ms: float
    error: Optional[str] = None
    metrics: Dict[str, float] = field(default_factory=dict)

@dataclass
class EvalSuite:
    name: str
    cases: List[EvalCase]
    metadata: Dict[str, Any] = field(default_factory=dict)

@dataclass
class EvalReport:
    suite_name: str
    results: List[EvalResult]
    pass_rate: float
    avg_score: float
    avg_latency_ms: float
    failed_cases: List[str]
    timestamp: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

class EvaluationHarness:
    """
    Asynchronous Evaluation Harness for benchmarking LLM models and pipelines.
    Supports custom metrics and suite registration.
    """

    def __init__(self):
        self.suites: Dict[str, EvalSuite] = {}
        self.metrics: Dict[str, Callable[[str, str], float]] = {
            "exact_match": self._metric_exact_match,
            "contains_keywords": self._metric_contains_keywords,
            "length_ratio": self._metric_length_ratio,
            "json_valid": self._metric_json_valid,
            "no_hallucination_markers": self._metric_no_hallucination
        }

    def register_suite(self, suite: EvalSuite):
        """Registers a new evaluation suite."""
        self.suites[suite.name] = suite
        logger.info(f"Registered suite: {suite.name} ({len(suite.cases)} cases)")

    def register_metric(self, name: str, fn: Callable[[str, str], float]):
        """Registers a custom metric function."""
        self.metrics[name] = fn
        logger.info(f"Registered metric: {name}")

    # Built-in Metrics
    def _metric_exact_match(self, actual: str, expected: str) -> float:
        return 1.0 if actual.strip() == expected.strip() else 0.0

    def _metric_contains_keywords(self, actual: str, expected: str) -> float:
        keywords = [k.strip() for k in expected.split(",")]
        if not keywords: return 1.0
        found = sum(1 for k in keywords if k.lower() in actual.lower())
        return found / len(keywords)

    def _metric_length_ratio(self, actual: str, expected: str) -> float:
        # Penalize if much longer or shorter than expected length (provided as int in expected)
        try:
            target_len = int(expected)
            actual_len = len(actual)
            ratio = min(actual_len, target_len) / max(actual_len, target_len)
            return ratio
        except ValueError:
            return 1.0

    def _metric_json_valid(self, actual: str, expected: str) -> float:
        try:
            json.loads(actual)
            return 1.0
        except json.JSONDecodeError:
            return 0.0

    def _metric_no_hallucination(self, actual: str, expected: str) -> float:
        markers = ["i think", "maybe", "not sure", "unclear", "hallucination"]
        found = sum(1 for m in markers if m in actual.lower())
        return max(0.0, 1.0 - (found * 0.2))

    async def run_case(self, case: EvalCase, llm_fn: Callable[[str], Any]) -> EvalResult:
        """Executes a single evaluation case."""
        start_time = time.time()
        error = None
        actual_output = ""
        
        try:
            # llm_fn is an async function or a function returning a future
            if asyncio.iscoroutinefunction(llm_fn):
                actual_output = await llm_fn(case.prompt)
            else:
                actual_output = llm_fn(case.prompt)
        except Exception as e:
            error = str(e)
            logger.error(f"Error in case {case.id}: {error}")
            
        latency = (time.time() - start_time) * 1000
        
        # Calculate scores using registered metrics
        case_metrics = {}
        # If metadata specifies metrics, use those, otherwise use all
        target_metrics = case.metadata.get("metrics", self.metrics.keys())
        
        for m_name in target_metrics:
            if m_name in self.metrics:
                case_metrics[m_name] = self.metrics[m_name](actual_output, case.expected_output)
        
        avg_score = sum(case_metrics.values()) / len(case_metrics) if case_metrics else 0.0
        passed = avg_score >= case.metadata.get("pass_threshold", 0.7)
        
        return EvalResult(
            case_id=case.id,
            actual_output=actual_output,
            score=avg_score,
            passed=passed,
            latency_ms=latency,
            error=error,
            metrics=case_metrics
        )

    async def run_suite(self, suite_name: str, llm_fn: Callable[[str], Any]) -> EvalReport:
        """Runs an entire suite of test cases."""
        if suite_name not in self.suites:
            raise ValueError(f"Suite {suite_name} not found")
            
        suite = self.suites[suite_name]
        logger.info(f"Running suite: {suite_name}")
        
        tasks = [self.run_case(case, llm_fn) for case in suite.cases]
        results = await asyncio.gather(*tasks)
        
        total_score = sum(r.score for r in results)
        total_latency = sum(r.latency_ms for r in results)
        passed_count = sum(1 for r in results if r.passed)
        failed_cases = [r.case_id for r in results if not r.passed]
        
        report = EvalReport(
            suite_name=suite_name,
            results=results,
            pass_rate=passed_count / len(results),
            avg_score=total_score / len(results),
            avg_latency_ms=total_latency / len(results),
            failed_cases=failed_cases
        )
        
        # Persist report
        path = f"/tmp/jarvis_eval_{suite_name}_{int(time.time())}.json"
        with open(path, "w") as f:
            f.write(report.to_json())
            
        logger.info(f"Suite {suite_name} complete. Pass rate: {report.pass_rate:.2%}")
        return report

    def compare_reports(self, report_a: EvalReport, report_b: EvalReport) -> Dict[str, Any]:
        """Compares two reports to detect improvements or regressions."""
        return {
            "pass_rate_delta": report_b.pass_rate - report_a.pass_rate,
            "avg_score_delta": report_b.avg_score - report_a.avg_score,
            "latency_delta_pct": (report_b.avg_latency_ms - report_a.avg_latency_ms) / report_a.avg_latency_ms if report_a.avg_latency_ms > 0 else 0
        }

def build_jarvis_evaluation_harness() -> EvaluationHarness:
    """Factory function."""
    return EvaluationHarness()

# Mock LLM for demo
async def mock_llm(prompt: str) -> str:
    await asyncio.sleep(0.1) # Simulate latency
    if "status" in prompt.lower():
        return "The cluster is online and all 6 GPUs are healthy."
    if "json" in prompt.lower():
        return '{"status": "ok", "nodes": 12}'
    return "I am not sure about that, maybe check the logs."

async def demo():
    harness = build_jarvis_evaluation_harness()
    
    # Create a demo suite
    cases = [
        EvalCase("H-001", "What is the status of the cluster?", "online, healthy", ["health"], metadata={"metrics": ["contains_keywords"]}),
        EvalCase("J-001", "Return a JSON status object", '{"status": "ok"}', ["format"], metadata={"metrics": ["json_valid"]}),
        EvalCase("U-001", "Is the system stable?", "stable", ["logic"], metadata={"pass_threshold": 0.9})
    ]
    
    suite = EvalSuite("ClusterHealth", cases)
    harness.register_suite(suite)
    
    report = await harness.run_suite("ClusterHealth", mock_llm)
    
    print(f"\n{'REPORT FOR':<20}: {report.suite_name}")
    print(f"{'PASS RATE':<20}: {report.pass_rate:.2%}")
    print(f"{'AVG SCORE':<20}: {report.avg_score:.4f}")
    print(f"{'AVG LATENCY':<20}: {report.avg_latency_ms:.2f} ms")
    print("-" * 40)
    
    for res in report.results:
        status = "✅ PASS" if res.passed else "❌ FAIL"
        print(f"[{res.case_id}] {status} | Score: {res.score:.2f} | Latency: {res.latency_ms:.1f}ms")

if __name__ == "__main__":
    asyncio.run(demo())
