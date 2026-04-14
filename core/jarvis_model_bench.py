#!/usr/bin/env python3
"""
jarvis_model_bench — LLM benchmarking suite with standard tasks and scoring
MMLU-style probes, code generation, summarization, latency profiling
"""

import asyncio
import json
import logging
import statistics
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.model_bench")

RESULTS_FILE = Path("/home/turbo/IA/Core/jarvis/data/bench_results.jsonl")
REDIS_PREFIX = "jarvis:bench:"


class BenchCategory(str, Enum):
    KNOWLEDGE = "knowledge"
    REASONING = "reasoning"
    CODING = "coding"
    SUMMARIZATION = "summarization"
    INSTRUCTION = "instruction"
    MATH = "math"
    LATENCY = "latency"


@dataclass
class BenchCase:
    case_id: str
    category: BenchCategory
    prompt: str
    expected: str  # keyword or exact match
    match_mode: str = "contains"  # "contains" | "exact" | "all_keywords"
    keywords: list[str] = field(default_factory=list)
    max_tokens: int = 128
    weight: float = 1.0


@dataclass
class BenchRunResult:
    case_id: str
    category: str
    model: str
    latency_ms: float
    output_tokens: int
    response: str
    score: float  # 0.0 or 1.0 for most tasks
    passed: bool
    error: str = ""

    @property
    def tps(self) -> float:
        return (
            self.output_tokens / (self.latency_ms / 1000)
            if self.latency_ms > 0
            else 0.0
        )

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "category": self.category,
            "model": self.model,
            "latency_ms": round(self.latency_ms, 1),
            "output_tokens": self.output_tokens,
            "tps": round(self.tps, 1),
            "score": round(self.score, 3),
            "passed": self.passed,
            "error": self.error[:100] if self.error else "",
        }


@dataclass
class BenchSuiteResult:
    model: str
    endpoint_url: str
    total_cases: int
    passed: int
    failed: int
    errored: int
    avg_latency_ms: float
    p50_latency_ms: float
    p95_latency_ms: float
    avg_tps: float
    accuracy: float  # passed / (passed + failed)
    weighted_score: float  # weighted avg
    by_category: dict[str, dict] = field(default_factory=dict)
    run_results: list[BenchRunResult] = field(default_factory=list)
    ts: float = field(default_factory=time.time)
    duration_s: float = 0.0

    def to_dict(self, include_runs: bool = False) -> dict:
        d = {
            "model": self.model,
            "endpoint_url": self.endpoint_url,
            "total_cases": self.total_cases,
            "passed": self.passed,
            "failed": self.failed,
            "errored": self.errored,
            "accuracy": round(self.accuracy, 3),
            "weighted_score": round(self.weighted_score, 3),
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "p50_latency_ms": round(self.p50_latency_ms, 1),
            "p95_latency_ms": round(self.p95_latency_ms, 1),
            "avg_tps": round(self.avg_tps, 1),
            "by_category": self.by_category,
            "duration_s": round(self.duration_s, 2),
            "ts": self.ts,
        }
        if include_runs:
            d["runs"] = [r.to_dict() for r in self.run_results]
        return d


def _score_case(case: BenchCase, response: str) -> float:
    if case.match_mode == "exact":
        return 1.0 if response.strip() == case.expected.strip() else 0.0
    elif case.match_mode == "contains":
        return 1.0 if case.expected.lower() in response.lower() else 0.0
    elif case.match_mode == "all_keywords":
        if not case.keywords:
            return 1.0
        hits = sum(1 for kw in case.keywords if kw.lower() in response.lower())
        return hits / len(case.keywords)
    return 0.0


async def _infer(
    endpoint_url: str,
    model: str,
    prompt: str,
    max_tokens: int,
    timeout_s: float = 45.0,
) -> tuple[str, float, int]:
    """Returns (response, latency_ms, output_tokens)."""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.0,
    }
    t0 = time.time()
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=timeout_s)
    ) as sess:
        async with sess.post(f"{endpoint_url}/v1/chat/completions", json=payload) as r:
            latency_ms = (time.time() - t0) * 1000
            if r.status != 200:
                text = await r.text()
                raise RuntimeError(f"HTTP {r.status}: {text[:80]}")
            data = await r.json(content_type=None)
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            usage = data.get("usage", {})
            out_tokens = usage.get("completion_tokens", max(1, len(content) // 4))
            return content, latency_ms, out_tokens


class ModelBench:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._cases: list[BenchCase] = []
        self._results: dict[str, BenchSuiteResult] = {}
        self._stats: dict[str, int] = {
            "suites_run": 0,
            "cases_run": 0,
            "cases_passed": 0,
        }
        RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._load_default_cases()

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def _load_default_cases(self):
        self._cases = [
            # Knowledge
            BenchCase(
                "k01",
                BenchCategory.KNOWLEDGE,
                "What is the capital of France? Answer in one word.",
                "Paris",
            ),
            BenchCase(
                "k02",
                BenchCategory.KNOWLEDGE,
                "Who wrote the theory of relativity? Last name only.",
                "Einstein",
            ),
            BenchCase(
                "k03",
                BenchCategory.KNOWLEDGE,
                "What element has atomic number 79? Answer: element name only.",
                "gold",
                match_mode="contains",
            ),
            # Math
            BenchCase(
                "m01",
                BenchCategory.MATH,
                "What is 17 × 13? Answer with the number only.",
                "221",
            ),
            BenchCase(
                "m02",
                BenchCategory.MATH,
                "What is the square root of 144? Answer with the number only.",
                "12",
            ),
            BenchCase(
                "m03",
                BenchCategory.MATH,
                "If x² = 81, what is x? Give positive value only.",
                "9",
            ),
            # Reasoning
            BenchCase(
                "r01",
                BenchCategory.REASONING,
                "All cats are animals. Whiskers is a cat. Is Whiskers an animal? Yes or No.",
                "yes",
                match_mode="contains",
            ),
            BenchCase(
                "r02",
                BenchCategory.REASONING,
                "If today is Wednesday, what day was it 3 days ago? One word.",
                "sunday",
                match_mode="contains",
            ),
            # Coding
            BenchCase(
                "c01",
                BenchCategory.CODING,
                "Write a Python one-liner that prints numbers 1 to 5.",
                "",
                match_mode="all_keywords",
                keywords=["print", "range"],
                max_tokens=64,
            ),
            BenchCase(
                "c02",
                BenchCategory.CODING,
                "What Python function reverses a list in place? Function name only.",
                "reverse",
            ),
            # Instruction following
            BenchCase(
                "i01",
                BenchCategory.INSTRUCTION,
                "Reply with exactly the word: PINEAPPLE",
                "PINEAPPLE",
                match_mode="contains",
            ),
            BenchCase(
                "i02",
                BenchCategory.INSTRUCTION,
                "Count to 5, separated by commas, nothing else.",
                "",
                match_mode="all_keywords",
                keywords=["1", "2", "3", "4", "5"],
                max_tokens=32,
            ),
        ]

    def add_case(self, case: BenchCase):
        self._cases.append(case)

    async def _run_case(
        self, case: BenchCase, model: str, endpoint_url: str
    ) -> BenchRunResult:
        try:
            response, latency_ms, out_tokens = await _infer(
                endpoint_url, model, case.prompt, case.max_tokens
            )
            score = _score_case(case, response)
            return BenchRunResult(
                case_id=case.case_id,
                category=case.category.value,
                model=model,
                latency_ms=latency_ms,
                output_tokens=out_tokens,
                response=response,
                score=score,
                passed=score >= 0.5,
            )
        except Exception as e:
            return BenchRunResult(
                case_id=case.case_id,
                category=case.category.value,
                model=model,
                latency_ms=0.0,
                output_tokens=0,
                response="",
                score=0.0,
                passed=False,
                error=str(e)[:200],
            )

    async def run(
        self,
        model: str,
        endpoint_url: str,
        categories: list[BenchCategory] | None = None,
        concurrency: int = 3,
    ) -> BenchSuiteResult:
        cases = self._cases
        if categories:
            cases = [c for c in cases if c.category in categories]

        t0 = time.time()
        sem = asyncio.Semaphore(concurrency)

        async def bounded(c: BenchCase) -> BenchRunResult:
            async with sem:
                return await self._run_case(c, model, endpoint_url)

        runs = await asyncio.gather(*[bounded(c) for c in cases])
        duration = time.time() - t0

        self._stats["suites_run"] += 1
        self._stats["cases_run"] += len(runs)

        success_runs = [r for r in runs if not r.error]
        error_runs = [r for r in runs if r.error]
        passed_runs = [r for r in success_runs if r.passed]

        self._stats["cases_passed"] += len(passed_runs)

        latencies = [r.latency_ms for r in success_runs] or [0.0]
        tps_list = [r.tps for r in success_runs] or [0.0]
        lat_sorted = sorted(latencies)
        p50 = statistics.median(lat_sorted)
        p95 = lat_sorted[min(int(len(lat_sorted) * 0.95), len(lat_sorted) - 1)]

        # Weighted score
        case_weight = {c.case_id: c.weight for c in cases}
        total_weight = sum(case_weight.values())
        weighted = sum(r.score * case_weight.get(r.case_id, 1.0) for r in success_runs)
        weighted_score = weighted / max(total_weight, 1.0)

        accuracy = len(passed_runs) / max(len(success_runs), 1)

        # By category
        by_cat: dict[str, dict[str, Any]] = {}
        for r in success_runs:
            cat = r.category
            if cat not in by_cat:
                by_cat[cat] = {"passed": 0, "total": 0, "avg_latency_ms": 0.0}
            by_cat[cat]["total"] += 1
            if r.passed:
                by_cat[cat]["passed"] += 1
        for cat_data in by_cat.values():
            cat_data["accuracy"] = round(
                cat_data["passed"] / max(cat_data["total"], 1), 3
            )

        result = BenchSuiteResult(
            model=model,
            endpoint_url=endpoint_url,
            total_cases=len(runs),
            passed=len(passed_runs),
            failed=len(success_runs) - len(passed_runs),
            errored=len(error_runs),
            avg_latency_ms=sum(latencies) / max(len(latencies), 1),
            p50_latency_ms=p50,
            p95_latency_ms=p95,
            avg_tps=sum(tps_list) / max(len(tps_list), 1),
            accuracy=accuracy,
            weighted_score=weighted_score,
            by_category=by_cat,
            run_results=list(runs),
            duration_s=duration,
        )
        self._results[model] = result

        try:
            with open(RESULTS_FILE, "a") as f:
                f.write(json.dumps(result.to_dict()) + "\n")
        except Exception:
            pass

        if self.redis:
            asyncio.create_task(
                self.redis.setex(
                    f"{REDIS_PREFIX}{model}",
                    86400,
                    json.dumps(result.to_dict()),
                )
            )

        log.info(
            f"Bench {model}: accuracy={accuracy:.1%} score={weighted_score:.3f} "
            f"lat={result.avg_latency_ms:.0f}ms ({duration:.1f}s)"
        )
        return result

    def leaderboard(self) -> list[dict]:
        return sorted(
            [r.to_dict() for r in self._results.values()],
            key=lambda r: -r["weighted_score"],
        )

    def stats(self) -> dict:
        return {
            **self._stats,
            "total_cases_available": len(self._cases),
            "models_benchmarked": len(self._results),
        }


def build_jarvis_model_bench() -> ModelBench:
    return ModelBench()


async def main():
    import sys

    bench = build_jarvis_model_bench()
    await bench.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"

    if cmd == "run":
        model = sys.argv[2] if len(sys.argv) > 2 else "qwen3.5-9b"
        endpoint = sys.argv[3] if len(sys.argv) > 3 else "http://192.168.1.85:1234"

        print(f"Benchmarking {model} @ {endpoint} ({len(bench._cases)} cases)...")
        result = await bench.run(model, endpoint, concurrency=3)

        print(f"\nResults for {model}:")
        print(
            f"  Accuracy:       {result.accuracy:.1%} ({result.passed}/{result.total_cases})"
        )
        print(f"  Weighted score: {result.weighted_score:.3f}")
        print(f"  Avg latency:    {result.avg_latency_ms:.0f}ms")
        print(f"  P95 latency:    {result.p95_latency_ms:.0f}ms")
        print(f"  Avg TPS:        {result.avg_tps:.1f}")
        print(f"  Duration:       {result.duration_s:.1f}s")

        print("\nBy category:")
        for cat, data in result.by_category.items():
            print(
                f"  {cat:<15} {data['passed']}/{data['total']} ({data['accuracy']:.0%})"
            )

        print("\nFailed cases:")
        for r in result.run_results:
            if not r.passed:
                print(f"  [{r.case_id}] {r.error or r.response[:60]!r}")

    elif cmd == "leaderboard":
        for entry in bench.leaderboard():
            print(
                f"{entry['model']:<30} acc={entry['accuracy']:.1%} "
                f"score={entry['weighted_score']:.3f} lat={entry['avg_latency_ms']:.0f}ms"
            )

    elif cmd == "stats":
        print(json.dumps(bench.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
