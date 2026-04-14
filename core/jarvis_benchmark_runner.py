#!/usr/bin/env python3
"""
jarvis_benchmark_runner — LLM and system benchmark suite runner
Latency, throughput, accuracy, and resource benchmarks with report generation
"""

import asyncio
import json
import logging
import statistics
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

log = logging.getLogger("jarvis.benchmark_runner")


class BenchmarkKind(str, Enum):
    LATENCY = "latency"
    THROUGHPUT = "throughput"
    ACCURACY = "accuracy"
    MEMORY = "memory"
    CONCURRENCY = "concurrency"
    REGRESSION = "regression"


class BenchmarkStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class BenchmarkCase:
    name: str
    kind: BenchmarkKind
    fn: Callable
    warmup_runs: int = 3
    iterations: int = 20
    concurrency: int = 1
    timeout_s: float = 60.0
    tags: list[str] = field(default_factory=list)
    baseline: dict[str, float] | None = None  # for regression comparison


@dataclass
class RunMetrics:
    latencies_ms: list[float] = field(default_factory=list)
    errors: int = 0
    throughput_rps: float = 0.0
    total_s: float = 0.0
    memory_mb: float = 0.0

    @property
    def p50(self) -> float:
        return statistics.median(self.latencies_ms) if self.latencies_ms else 0.0

    @property
    def p95(self) -> float:
        if not self.latencies_ms:
            return 0.0
        s = sorted(self.latencies_ms)
        return s[min(int(len(s) * 0.95), len(s) - 1)]

    @property
    def p99(self) -> float:
        if not self.latencies_ms:
            return 0.0
        s = sorted(self.latencies_ms)
        return s[min(int(len(s) * 0.99), len(s) - 1)]

    @property
    def mean(self) -> float:
        return statistics.mean(self.latencies_ms) if self.latencies_ms else 0.0

    @property
    def stddev(self) -> float:
        return (
            statistics.stdev(self.latencies_ms) if len(self.latencies_ms) >= 2 else 0.0
        )

    @property
    def error_rate(self) -> float:
        total = len(self.latencies_ms) + self.errors
        return self.errors / max(total, 1)

    def to_dict(self) -> dict:
        return {
            "n": len(self.latencies_ms),
            "errors": self.errors,
            "error_rate": round(self.error_rate, 4),
            "mean_ms": round(self.mean, 2),
            "p50_ms": round(self.p50, 2),
            "p95_ms": round(self.p95, 2),
            "p99_ms": round(self.p99, 2),
            "stddev_ms": round(self.stddev, 2),
            "throughput_rps": round(self.throughput_rps, 2),
            "total_s": round(self.total_s, 3),
            "memory_mb": round(self.memory_mb, 2),
        }


@dataclass
class BenchmarkResult:
    case_name: str
    kind: BenchmarkKind
    status: BenchmarkStatus
    metrics: RunMetrics = field(default_factory=RunMetrics)
    regression_flags: list[str] = field(default_factory=list)
    error: str = ""
    ts: float = field(default_factory=time.time)

    @property
    def passed(self) -> bool:
        return self.status == BenchmarkStatus.DONE and not self.regression_flags

    def to_dict(self) -> dict:
        return {
            "case": self.case_name,
            "kind": self.kind.value,
            "status": self.status.value,
            "passed": self.passed,
            "metrics": self.metrics.to_dict(),
            "regression_flags": self.regression_flags,
            "error": self.error,
            "ts": self.ts,
        }


class BenchmarkRunner:
    def __init__(self):
        self._cases: list[BenchmarkCase] = []
        self._results: list[BenchmarkResult] = []
        self._regression_threshold_pct: float = 20.0  # flag if >20% worse than baseline
        self._stats: dict[str, int] = {
            "cases_run": 0,
            "passed": 0,
            "failed": 0,
            "regressions": 0,
        }

    def add(self, case: BenchmarkCase):
        self._cases.append(case)

    def set_regression_threshold(self, pct: float):
        self._regression_threshold_pct = pct

    async def _run_case(self, case: BenchmarkCase) -> BenchmarkResult:
        result = BenchmarkResult(
            case_name=case.name,
            kind=case.kind,
            status=BenchmarkStatus.RUNNING,
        )

        try:
            # Warmup
            for _ in range(case.warmup_runs):
                try:
                    coro = case.fn()
                    if asyncio.iscoroutine(coro):
                        await asyncio.wait_for(coro, timeout=case.timeout_s)
                    else:
                        coro
                except Exception:
                    pass

            metrics = RunMetrics()
            sem = asyncio.Semaphore(case.concurrency)

            async def _one_run() -> float:
                async with sem:
                    t0 = time.perf_counter()
                    try:
                        coro = case.fn()
                        if asyncio.iscoroutine(coro):
                            await asyncio.wait_for(coro, timeout=case.timeout_s)
                        else:
                            coro
                        return (time.perf_counter() - t0) * 1000
                    except Exception:
                        metrics.errors += 1
                        return -1.0

            wall_start = time.time()

            if case.concurrency > 1:
                tasks = [
                    asyncio.create_task(_one_run()) for _ in range(case.iterations)
                ]
                latencies = await asyncio.gather(*tasks)
            else:
                latencies = []
                for _ in range(case.iterations):
                    ms = await _one_run()
                    latencies.append(ms)

            wall_total = time.time() - wall_start

            metrics.latencies_ms = [ms for ms in latencies if ms >= 0]
            metrics.total_s = wall_total
            successful = len(metrics.latencies_ms)
            metrics.throughput_rps = successful / max(wall_total, 0.001)

            result.metrics = metrics
            result.status = BenchmarkStatus.DONE

            # Regression check
            if case.baseline:
                bl = case.baseline
                flags = []
                if "p95_ms" in bl and metrics.p95 > bl["p95_ms"] * (
                    1 + self._regression_threshold_pct / 100
                ):
                    flags.append(
                        f"p95 regression: {metrics.p95:.1f}ms vs baseline {bl['p95_ms']:.1f}ms"
                    )
                if "error_rate" in bl and metrics.error_rate > bl["error_rate"] + 0.05:
                    flags.append(
                        f"error_rate regression: {metrics.error_rate:.3f} vs {bl['error_rate']:.3f}"
                    )
                if "throughput_rps" in bl and metrics.throughput_rps < bl[
                    "throughput_rps"
                ] * (1 - self._regression_threshold_pct / 100):
                    flags.append(
                        f"throughput regression: {metrics.throughput_rps:.1f} vs {bl['throughput_rps']:.1f} rps"
                    )
                result.regression_flags = flags
                if flags:
                    self._stats["regressions"] += 1

        except Exception as e:
            result.status = BenchmarkStatus.FAILED
            result.error = str(e)
            log.error(f"Benchmark {case.name!r} failed: {e}")

        return result

    async def run(
        self,
        names: list[str] | None = None,
        kinds: list[BenchmarkKind] | None = None,
        tags: list[str] | None = None,
    ) -> list[BenchmarkResult]:
        cases = self._cases
        if names:
            cases = [c for c in cases if c.name in names]
        if kinds:
            cases = [c for c in cases if c.kind in kinds]
        if tags:
            cases = [c for c in cases if any(t in c.tags for t in tags)]

        results = []
        for case in cases:
            log.info(f"Running benchmark: {case.name!r}")
            r = await self._run_case(case)
            self._results.append(r)
            results.append(r)
            self._stats["cases_run"] += 1
            if r.passed:
                self._stats["passed"] += 1
            elif r.status == BenchmarkStatus.FAILED:
                self._stats["failed"] += 1

        return results

    def report(self, results: list[BenchmarkResult] | None = None) -> dict:
        recs = results or self._results
        by_kind: dict[str, list[dict]] = {}
        for r in recs:
            by_kind.setdefault(r.kind.value, []).append(r.to_dict())

        regressions = [r.case_name for r in recs if r.regression_flags]
        failed = [r.case_name for r in recs if r.status == BenchmarkStatus.FAILED]

        return {
            "total": len(recs),
            "passed": sum(1 for r in recs if r.passed),
            "failed": failed,
            "regressions": regressions,
            "by_kind": {k: len(v) for k, v in by_kind.items()},
            "results": [r.to_dict() for r in recs],
        }

    def stats(self) -> dict:
        return {**self._stats}


def build_jarvis_benchmark_runner() -> BenchmarkRunner:
    runner = BenchmarkRunner()

    # Inference latency benchmark
    async def mock_inference():
        import random

        await asyncio.sleep(random.uniform(0.05, 0.15))
        return {"tokens": 100, "model": "qwen3.5"}

    runner.add(
        BenchmarkCase(
            name="inference.latency",
            kind=BenchmarkKind.LATENCY,
            fn=mock_inference,
            warmup_runs=3,
            iterations=20,
            baseline={"p95_ms": 200.0, "error_rate": 0.0},
            tags=["inference", "latency"],
        )
    )

    # Throughput benchmark
    async def mock_fast():
        await asyncio.sleep(0.01)
        return True

    runner.add(
        BenchmarkCase(
            name="inference.throughput",
            kind=BenchmarkKind.THROUGHPUT,
            fn=mock_fast,
            warmup_runs=2,
            iterations=50,
            concurrency=4,
            baseline={"throughput_rps": 50.0},
            tags=["inference", "throughput"],
        )
    )

    # Redis latency
    async def mock_redis():
        await asyncio.sleep(0.002)
        return "PONG"

    runner.add(
        BenchmarkCase(
            name="redis.ping_latency",
            kind=BenchmarkKind.LATENCY,
            fn=mock_redis,
            warmup_runs=5,
            iterations=100,
            baseline={"p95_ms": 10.0},
            tags=["redis", "infra"],
        )
    )

    return runner


async def main():
    import sys

    runner = build_jarvis_benchmark_runner()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Benchmark runner demo...\n")
        results = await runner.run()

        for r in results:
            icon = (
                "✅"
                if r.passed
                else ("❌" if r.status == BenchmarkStatus.FAILED else "⚠️")
            )
            m = r.metrics
            print(
                f"  {icon} {r.case_name:<30} mean={m.mean:.1f}ms p95={m.p95:.1f}ms rps={m.throughput_rps:.1f} err={m.error_rate:.3f}"
            )
            for flag in r.regression_flags:
                print(f"    ⚠ {flag}")

        print(
            f"\nReport summary: {json.dumps({k: v for k, v in runner.report(results).items() if k != 'results'}, indent=2)}"
        )

    elif cmd == "stats":
        print(json.dumps(runner.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
