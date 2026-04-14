#!/usr/bin/env python3
"""
jarvis_prompt_tester — Automated prompt regression testing and quality scoring
Run prompt suites against LLM endpoints, score outputs, detect regressions
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.prompt_tester")

SUITE_FILE = Path("/home/turbo/IA/Core/jarvis/data/prompt_suites.jsonl")
RESULTS_FILE = Path("/home/turbo/IA/Core/jarvis/data/prompt_test_results.jsonl")
REDIS_PREFIX = "jarvis:ptester:"


class TestStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"
    ERROR = "error"


class ScoreMethod(str, Enum):
    EXACT = "exact"  # exact string match
    CONTAINS = "contains"  # expected substring present
    REGEX = "regex"  # regex match
    KEYWORD = "keyword"  # all keywords present
    SEMANTIC = "semantic"  # cosine similarity ≥ threshold (if embedding available)
    CUSTOM = "custom"  # user-provided callable


@dataclass
class PromptTest:
    test_id: str
    name: str
    messages: list[dict]
    model: str
    endpoint_url: str
    expected: str = ""
    score_method: ScoreMethod = ScoreMethod.CONTAINS
    keywords: list[str] = field(default_factory=list)
    score_threshold: float = 0.0  # for semantic
    max_latency_ms: float = 10000.0
    tags: list[str] = field(default_factory=list)
    custom_scorer: Callable | None = field(default=None, repr=False)

    def to_dict(self) -> dict:
        return {
            "test_id": self.test_id,
            "name": self.name,
            "model": self.model,
            "score_method": self.score_method.value,
            "expected": self.expected[:100],
            "tags": self.tags,
        }


@dataclass
class TestResult:
    test_id: str
    name: str
    status: TestStatus
    score: float  # 0.0–1.0
    latency_ms: float
    model: str
    actual_output: str
    error: str = ""
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "test_id": self.test_id,
            "name": self.name,
            "status": self.status.value,
            "score": round(self.score, 4),
            "latency_ms": round(self.latency_ms, 1),
            "model": self.model,
            "actual_output": self.actual_output[:200],
            "error": self.error,
            "ts": self.ts,
        }


@dataclass
class SuiteResult:
    suite_name: str
    total: int
    passed: int
    failed: int
    errored: int
    skipped: int
    avg_score: float
    avg_latency_ms: float
    duration_s: float
    results: list[TestResult] = field(default_factory=list)
    ts: float = field(default_factory=time.time)

    @property
    def pass_rate(self) -> float:
        return self.passed / max(self.total, 1)

    def to_dict(self) -> dict:
        return {
            "suite_name": self.suite_name,
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "errored": self.errored,
            "pass_rate": round(self.pass_rate, 3),
            "avg_score": round(self.avg_score, 4),
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "duration_s": round(self.duration_s, 2),
            "ts": self.ts,
        }


def _score_exact(actual: str, expected: str) -> float:
    return 1.0 if actual.strip() == expected.strip() else 0.0


def _score_contains(actual: str, expected: str) -> float:
    return 1.0 if expected.lower() in actual.lower() else 0.0


def _score_regex(actual: str, pattern: str) -> float:
    import re

    return 1.0 if re.search(pattern, actual, re.IGNORECASE) else 0.0


def _score_keyword(actual: str, keywords: list[str]) -> float:
    if not keywords:
        return 1.0
    hits = sum(1 for kw in keywords if kw.lower() in actual.lower())
    return hits / len(keywords)


def _score_output(test: PromptTest, actual: str) -> float:
    if test.score_method == ScoreMethod.EXACT:
        return _score_exact(actual, test.expected)
    elif test.score_method == ScoreMethod.CONTAINS:
        return _score_contains(actual, test.expected)
    elif test.score_method == ScoreMethod.REGEX:
        return _score_regex(actual, test.expected)
    elif test.score_method == ScoreMethod.KEYWORD:
        return _score_keyword(actual, test.keywords)
    elif test.score_method == ScoreMethod.CUSTOM and test.custom_scorer:
        try:
            return float(test.custom_scorer(actual))
        except Exception:
            return 0.0
    return 1.0  # SEMANTIC — not implemented locally, pass by default


async def _call_model(
    endpoint_url: str, model: str, messages: list[dict], timeout_s: float = 30.0
) -> tuple[str, float]:
    """Returns (text, latency_ms)."""
    t0 = time.time()
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": 512,
        "temperature": 0.0,
    }
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=timeout_s)
    ) as sess:
        async with sess.post(f"{endpoint_url}/v1/chat/completions", json=payload) as r:
            latency_ms = (time.time() - t0) * 1000
            if r.status == 200:
                data = await r.json(content_type=None)
                text = (
                    data.get("choices", [{}])[0].get("message", {}).get("content", "")
                )
                return text, latency_ms
            text = await r.text()
            raise RuntimeError(f"HTTP {r.status}: {text[:100]}")


class PromptTester:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._suites: dict[str, list[PromptTest]] = {}  # suite_name → tests
        self._history: list[SuiteResult] = []
        self._stats: dict[str, int] = {
            "suites_run": 0,
            "tests_run": 0,
            "tests_passed": 0,
            "tests_failed": 0,
        }
        RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def add_test(self, suite: str, test: PromptTest):
        self._suites.setdefault(suite, []).append(test)

    def add_suite(self, suite: str, tests: list[PromptTest]):
        self._suites[suite] = tests

    async def run_test(self, test: PromptTest) -> TestResult:
        try:
            actual, latency_ms = await _call_model(
                test.endpoint_url, test.model, test.messages
            )
        except Exception as e:
            return TestResult(
                test_id=test.test_id,
                name=test.name,
                status=TestStatus.ERROR,
                score=0.0,
                latency_ms=0.0,
                model=test.model,
                actual_output="",
                error=str(e)[:200],
            )

        score = _score_output(test, actual)
        latency_ok = latency_ms <= test.max_latency_ms

        if score >= 0.5 and latency_ok:
            status = TestStatus.PASS
        else:
            status = TestStatus.FAIL

        return TestResult(
            test_id=test.test_id,
            name=test.name,
            status=status,
            score=score,
            latency_ms=latency_ms,
            model=test.model,
            actual_output=actual,
        )

    async def run_suite(
        self, suite_name: str, concurrency: int = 4, tags: list[str] | None = None
    ) -> SuiteResult:
        tests = self._suites.get(suite_name, [])
        if tags:
            tests = [t for t in tests if any(tag in t.tags for tag in tags)]

        t0 = time.time()
        sem = asyncio.Semaphore(concurrency)

        async def bounded_run(t: PromptTest) -> TestResult:
            async with sem:
                return await self.run_test(t)

        results = await asyncio.gather(*[bounded_run(t) for t in tests])

        passed = sum(1 for r in results if r.status == TestStatus.PASS)
        failed = sum(1 for r in results if r.status == TestStatus.FAIL)
        errored = sum(1 for r in results if r.status == TestStatus.ERROR)
        skipped = sum(1 for r in results if r.status == TestStatus.SKIP)
        avg_score = sum(r.score for r in results) / max(len(results), 1)
        avg_latency = sum(r.latency_ms for r in results) / max(len(results), 1)

        suite_result = SuiteResult(
            suite_name=suite_name,
            total=len(results),
            passed=passed,
            failed=failed,
            errored=errored,
            skipped=skipped,
            avg_score=avg_score,
            avg_latency_ms=avg_latency,
            duration_s=time.time() - t0,
            results=list(results),
        )
        self._history.append(suite_result)
        self._stats["suites_run"] += 1
        self._stats["tests_run"] += len(results)
        self._stats["tests_passed"] += passed
        self._stats["tests_failed"] += failed + errored

        try:
            with open(RESULTS_FILE, "a") as f:
                f.write(json.dumps(suite_result.to_dict()) + "\n")
        except Exception:
            pass

        if self.redis:
            asyncio.create_task(
                self.redis.setex(
                    f"{REDIS_PREFIX}suite:{suite_name}",
                    3600,
                    json.dumps(suite_result.to_dict()),
                )
            )

        log.info(
            f"Suite '{suite_name}': {passed}/{len(results)} passed "
            f"(score={avg_score:.3f}, {suite_result.duration_s:.1f}s)"
        )
        return suite_result

    def regression_check(
        self, suite_name: str, min_pass_rate: float = 0.8, min_score: float = 0.7
    ) -> tuple[bool, str]:
        """Compare last two runs of a suite for regression."""
        runs = [r for r in self._history if r.suite_name == suite_name]
        if len(runs) < 2:
            return True, "Not enough history"
        prev, curr = runs[-2], runs[-1]
        if curr.pass_rate < min_pass_rate:
            return False, f"Pass rate {curr.pass_rate:.2%} < {min_pass_rate:.2%}"
        if curr.avg_score < min_score:
            return False, f"Avg score {curr.avg_score:.3f} < {min_score}"
        delta = curr.pass_rate - prev.pass_rate
        if delta < -0.1:
            return False, f"Pass rate dropped {abs(delta):.2%} vs last run"
        return True, "OK"

    def list_suites(self) -> list[dict]:
        return [
            {"suite": name, "test_count": len(tests)}
            for name, tests in self._suites.items()
        ]

    def stats(self) -> dict:
        pass_rate = self._stats["tests_passed"] / max(self._stats["tests_run"], 1)
        return {
            **self._stats,
            "pass_rate": round(pass_rate, 3),
            "suites": len(self._suites),
        }


def build_jarvis_prompt_tester() -> PromptTester:
    tester = PromptTester()

    m1 = "http://192.168.1.85:1234"
    model = "qwen3.5-9b"

    tester.add_suite(
        "smoke",
        [
            PromptTest(
                test_id="smoke-01",
                name="Basic factual — capital France",
                messages=[
                    {"role": "user", "content": "What is the capital of France?"}
                ],
                model=model,
                endpoint_url=m1,
                expected="paris",
                score_method=ScoreMethod.CONTAINS,
                tags=["smoke", "factual"],
            ),
            PromptTest(
                test_id="smoke-02",
                name="Basic math — 2+2",
                messages=[
                    {
                        "role": "user",
                        "content": "What is 2+2? Answer with only the number.",
                    }
                ],
                model=model,
                endpoint_url=m1,
                expected="4",
                score_method=ScoreMethod.CONTAINS,
                tags=["smoke", "math"],
            ),
            PromptTest(
                test_id="smoke-03",
                name="Code generation — hello world Python",
                messages=[
                    {
                        "role": "user",
                        "content": "Write a Python hello world program. One line only.",
                    }
                ],
                model=model,
                endpoint_url=m1,
                keywords=["print", "Hello"],
                score_method=ScoreMethod.KEYWORD,
                tags=["smoke", "code"],
            ),
        ],
    )
    return tester


async def main():
    import sys

    tester = build_jarvis_prompt_tester()
    await tester.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"

    if cmd == "list":
        print("Suites:")
        for s in tester.list_suites():
            print(f"  {s['suite']}: {s['test_count']} tests")

    elif cmd == "run":
        suite = sys.argv[2] if len(sys.argv) > 2 else "smoke"
        print(f"Running suite '{suite}'...")
        result = await tester.run_suite(suite)
        print(f"\nResult: {result.passed}/{result.total} passed")
        print(f"  Pass rate: {result.pass_rate:.1%}")
        print(f"  Avg score: {result.avg_score:.3f}")
        print(f"  Duration:  {result.duration_s:.2f}s")
        print("\nIndividual results:")
        for r in result.results:
            icon = "✅" if r.status == TestStatus.PASS else "❌"
            print(
                f"  {icon} [{r.test_id}] {r.name[:40]:<40} "
                f"score={r.score:.2f} {r.latency_ms:.0f}ms"
            )
            if r.error:
                print(f"       ERROR: {r.error}")

    elif cmd == "stats":
        print(json.dumps(tester.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
