import dataclasses
from typing import List, Dict, Callable, Any, Optional
import asyncio
import json

@dataclasses.dataclass
class EvalCase:
    id: str
    prompt: str
    expected_output: str
    tags: List[str]
    weight: float = 1.0

@dataclasses.dataclass
class EvalResult:
    case_id: str
    actual_output: str
    score: float
    passed: bool
    latency_ms: float
    error: str = ''

@dataclasses.dataclass
class EvalSuite:
    name: str
    cases: List[EvalCase]
    metadata: Dict[str, Any]

@dataclasses.dataclass
class EvalReport:
    suite_name: str
    results: List[EvalResult]
    pass_rate: float
    avg_score: float
    avg_latency_ms: float
    failed_cases: List[str]

class EvaluationHarness:
    def __init__(self):
        self.suites = {}
        self.metrics = {}

    def register_suite(self, suite: EvalSuite):
        self.suites[suite.name] = suite

    def register_metric(self, name: str, fn: Callable[[str, str], float]):
        self.metrics[name] = fn

    async def run_suite(self, name: str, llm_fn) -> EvalReport:
        if name not in self.suites:
            raise ValueError(f"Suite {name} not found")
        
        suite = self.suites[name]
        results = []
        total_latency = 0.0
        passed_count = 0

        for case in suite.cases:
            start_time = asyncio.get_event_loop().time()
            try:
                actual_output = await llm_fn(case.prompt)
                latency_ms = (asyncio.get_event_loop().time() - start_time) * 1000
                score = self.metrics['exact_match'](case.expected_output, actual_output)
                passed = score == 1.0
                results.append(EvalResult(case.id, actual_output, score, passed, latency_ms))
                if passed:
                    passed_count += 1
            except Exception as e:
                latency_ms = (asyncio.get_event_loop().time() - start_time) * 1000
                results.append(EvalResult(case.id, '', 0.0, False, latency_ms, str(e)))

        pass_rate = passed_count / len(suite.cases)
        avg_score = sum(r.score for r in results) / len(results)
        avg_latency_ms = total_latency / len(results)
        failed_cases = [r.case_id for r in results if not r.passed]

        return EvalReport(
            suite_name=name,
            results=results,
            pass_rate=pass_rate,
            avg_score=avg_score,
            avg_latency_ms=avg_latency_ms,
            failed_cases=failed_cases
        )

    async def run_all(self, llm_fn) -> List[EvalReport]:
        reports = []
        for suite in self.suites.values():
            report = await self.run_suite(suite.name, llm_fn)
            reports.append(report)
        return reports

    @staticmethod
    def compare_reports(a: EvalReport, b: EvalReport) -> Dict[str, Any]:
        comparison = {
            'suite_name': a.suite_name,
            'pass_rate_diff': a.pass_rate - b.pass_rate,
            'avg_score_diff': a.avg_score - b.avg_score,
            'avg_latency_ms_diff': a.avg_latency_ms - b.avg_latency_ms,
            'failed_cases_added': list(set(b.failed_cases) - set(a.failed_cases)),
            'failed_cases_removed': list(set(a.failed_cases) - set(b.failed_cases))
        }
        return comparison

    @staticmethod
    def save_report(report: EvalReport, path: str):
        with open(path, 'w') as f:
            json.dump(dataclasses.asdict(report), f, indent=4)

def exact_match(expected: str, actual: str) -> float:
    return 1.0 if expected == actual else 0.0

def contains_keywords(expected: str, actual: str) -> float:
    keywords = set(expected.split())
    found_keywords = set(actual.split()) & keywords
    return len(found_keywords) / len(keywords)

def length_ratio(expected: str, actual: str) -> float:
    if not expected:
        return 1.0
    return len(actual) / len(expected)

def json_valid(expected: str, actual: str) -> float:
    try:
        json.loads(actual)
        return 1.0
    except json.JSONDecodeError:
        return 0.0

def build_jarvis_evaluation_harness() -> EvaluationHarness:
    harness = EvaluationHarness()
    harness.register_metric('exact_match', exact_match)
    harness.register_metric('contains_keywords', contains_keywords)
    harness.register_metric('length_ratio', length_ratio)
    harness.register_metric('json_valid', json_valid)
    return harness

async def mock_llm(prompt: str) -> str:
    # Mock implementation of a language model
    return "Mock response to " + prompt

async def main():
    harness = build_jarvis_evaluation_harness()
    
    suite = EvalSuite(
        name="Test Suite",
        cases=[
            EvalCase(id="1", prompt="What is 2+2?", expected_output="4"),
            EvalCase(id="2", prompt="Describe a cat.", expected_output="A cat is a small, typically furry, carnivorous mammal.")
        ],
        metadata={}
    )
    
    harness.register_suite(suite)
    
    report = await harness.run_suite("Test Suite", mock_llm)
    print(report)

if __name__ == "__main__":
    asyncio.run(main())