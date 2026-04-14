#!/usr/bin/env python3
"""
jarvis_model_evaluator — Multi-metric LLM model evaluation and benchmarking
Measures latency, throughput, quality scores, and token efficiency per model
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

log = logging.getLogger("jarvis.model_evaluator")

RESULTS_FILE = Path("/home/turbo/IA/Core/jarvis/data/model_eval_results.jsonl")
REDIS_PREFIX = "jarvis:modeleval:"


class EvalMetric(str, Enum):
    LATENCY = "latency"
    THROUGHPUT = "throughput"
    QUALITY = "quality"
    TOKEN_EFFICIENCY = "token_efficiency"
    AVAILABILITY = "availability"
    CONSISTENCY = "consistency"


@dataclass
class EvalTask:
    task_id: str
    prompt: str
    expected_keywords: list[str] = field(default_factory=list)
    max_tokens: int = 256
    temperature: float = 0.0
    weight: float = 1.0  # contribution to aggregate score


@dataclass
class ModelRunResult:
    model: str
    task_id: str
    latency_ms: float
    input_tokens: int
    output_tokens: int
    output_text: str
    keyword_score: float  # fraction of expected_keywords found
    error: str = ""
    ts: float = field(default_factory=time.time)

    @property
    def success(self) -> bool:
        return not self.error

    @property
    def tokens_per_second(self) -> float:
        if self.latency_ms <= 0:
            return 0.0
        return self.output_tokens / (self.latency_ms / 1000)

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "task_id": self.task_id,
            "latency_ms": round(self.latency_ms, 1),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "tokens_per_second": round(self.tokens_per_second, 1),
            "keyword_score": round(self.keyword_score, 3),
            "success": self.success,
            "error": self.error[:100] if self.error else "",
        }


@dataclass
class ModelEvalReport:
    model: str
    endpoint_url: str
    task_count: int
    success_count: int
    avg_latency_ms: float
    p50_latency_ms: float
    p95_latency_ms: float
    avg_tokens_per_s: float
    avg_keyword_score: float
    availability: float  # success_count / task_count
    composite_score: float  # weighted aggregate 0–100
    run_results: list[ModelRunResult] = field(default_factory=list)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "endpoint_url": self.endpoint_url,
            "task_count": self.task_count,
            "success_count": self.success_count,
            "availability": round(self.availability, 3),
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "p50_latency_ms": round(self.p50_latency_ms, 1),
            "p95_latency_ms": round(self.p95_latency_ms, 1),
            "avg_tokens_per_s": round(self.avg_tokens_per_s, 1),
            "avg_keyword_score": round(self.avg_keyword_score, 3),
            "composite_score": round(self.composite_score, 1),
            "ts": self.ts,
        }


def _keyword_score(text: str, keywords: list[str]) -> float:
    if not keywords:
        return 1.0
    hits = sum(1 for kw in keywords if kw.lower() in text.lower())
    return hits / len(keywords)


def _estimate_tokens(text: str) -> int:
    """Rough approximation: 1 token ≈ 4 chars."""
    return max(1, len(text) // 4)


async def _run_task(
    endpoint_url: str,
    model: str,
    task: EvalTask,
    timeout_s: float = 45.0,
) -> ModelRunResult:
    messages = [{"role": "user", "content": task.prompt}]
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": task.max_tokens,
        "temperature": task.temperature,
    }
    t0 = time.time()
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout_s)
        ) as sess:
            async with sess.post(
                f"{endpoint_url}/v1/chat/completions", json=payload
            ) as r:
                latency_ms = (time.time() - t0) * 1000
                if r.status != 200:
                    text = await r.text()
                    return ModelRunResult(
                        model=model,
                        task_id=task.task_id,
                        latency_ms=latency_ms,
                        input_tokens=0,
                        output_tokens=0,
                        output_text="",
                        keyword_score=0.0,
                        error=f"HTTP {r.status}: {text[:80]}",
                    )
                data = await r.json(content_type=None)
                content = (
                    data.get("choices", [{}])[0].get("message", {}).get("content", "")
                )
                usage = data.get("usage", {})
                in_tokens = usage.get("prompt_tokens", _estimate_tokens(task.prompt))
                out_tokens = usage.get("completion_tokens", _estimate_tokens(content))
                return ModelRunResult(
                    model=model,
                    task_id=task.task_id,
                    latency_ms=latency_ms,
                    input_tokens=in_tokens,
                    output_tokens=out_tokens,
                    output_text=content,
                    keyword_score=_keyword_score(content, task.expected_keywords),
                )
    except Exception as e:
        latency_ms = (time.time() - t0) * 1000
        return ModelRunResult(
            model=model,
            task_id=task.task_id,
            latency_ms=latency_ms,
            input_tokens=0,
            output_tokens=0,
            output_text="",
            keyword_score=0.0,
            error=str(e)[:200],
        )


def _compute_composite(
    availability: float,
    avg_latency_ms: float,
    avg_tps: float,
    avg_quality: float,
) -> float:
    """Composite score 0–100."""
    # availability: 0–1 → weight 25
    avail_score = availability * 25

    # latency: 0ms=25pts, 5000ms=0pts (linear clamp)
    lat_score = max(0.0, 25 * (1 - avg_latency_ms / 5000))

    # throughput: 0 tok/s=0, 50 tok/s=25pts
    tps_score = min(25.0, avg_tps / 50 * 25)

    # quality: keyword score 0–1 → 0–25
    quality_score = avg_quality * 25

    return avail_score + lat_score + tps_score + quality_score


class ModelEvaluator:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._tasks: list[EvalTask] = []
        self._reports: dict[str, ModelEvalReport] = {}
        self._stats: dict[str, Any] = {
            "evaluations": 0,
            "total_tasks_run": 0,
            "total_errors": 0,
        }
        RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def add_task(self, task: EvalTask):
        self._tasks.append(task)

    def set_tasks(self, tasks: list[EvalTask]):
        self._tasks = tasks

    async def evaluate_model(
        self,
        model: str,
        endpoint_url: str,
        tasks: list[EvalTask] | None = None,
        concurrency: int = 3,
    ) -> ModelEvalReport:
        task_list = tasks or self._tasks
        sem = asyncio.Semaphore(concurrency)

        async def bounded(t: EvalTask) -> ModelRunResult:
            async with sem:
                return await _run_task(endpoint_url, model, t)

        results = await asyncio.gather(*[bounded(t) for t in task_list])
        self._stats["total_tasks_run"] += len(results)

        successes = [r for r in results if r.success]
        errors = [r for r in results if not r.success]
        self._stats["total_errors"] += len(errors)

        latencies = [r.latency_ms for r in successes] or [0.0]
        tps_list = [r.tokens_per_second for r in successes] or [0.0]
        quality_list = [r.keyword_score for r in successes] or [0.0]

        latencies_sorted = sorted(latencies)
        p50 = statistics.median(latencies_sorted)
        p95_idx = min(int(len(latencies_sorted) * 0.95), len(latencies_sorted) - 1)
        p95 = latencies_sorted[p95_idx]

        availability = len(successes) / max(len(results), 1)
        avg_latency = sum(latencies) / max(len(latencies), 1)
        avg_tps = sum(tps_list) / max(len(tps_list), 1)
        avg_quality = sum(quality_list) / max(len(quality_list), 1)
        composite = _compute_composite(availability, avg_latency, avg_tps, avg_quality)

        report = ModelEvalReport(
            model=model,
            endpoint_url=endpoint_url,
            task_count=len(results),
            success_count=len(successes),
            avg_latency_ms=avg_latency,
            p50_latency_ms=p50,
            p95_latency_ms=p95,
            avg_tokens_per_s=avg_tps,
            avg_keyword_score=avg_quality,
            availability=availability,
            composite_score=composite,
            run_results=list(results),
        )
        self._reports[model] = report
        self._stats["evaluations"] += 1

        try:
            with open(RESULTS_FILE, "a") as f:
                f.write(json.dumps(report.to_dict()) + "\n")
        except Exception:
            pass

        if self.redis:
            asyncio.create_task(
                self.redis.setex(
                    f"{REDIS_PREFIX}{model}",
                    3600,
                    json.dumps(report.to_dict()),
                )
            )

        log.info(
            f"Eval {model}: score={composite:.1f}/100 "
            f"avail={availability:.1%} latency={avg_latency:.0f}ms tps={avg_tps:.1f}"
        )
        return report

    async def compare_models(
        self,
        models: list[tuple[str, str]],  # [(model, endpoint_url), ...]
        concurrency: int = 2,
    ) -> list[ModelEvalReport]:
        """Evaluate all models and return sorted by composite_score."""
        reports = await asyncio.gather(
            *[
                self.evaluate_model(model, url, concurrency=concurrency)
                for model, url in models
            ]
        )
        return sorted(reports, key=lambda r: -r.composite_score)

    def best_model(self) -> str | None:
        if not self._reports:
            return None
        return max(self._reports, key=lambda m: self._reports[m].composite_score)

    def leaderboard(self) -> list[dict]:
        return [
            r.to_dict()
            for r in sorted(self._reports.values(), key=lambda r: -r.composite_score)
        ]

    def stats(self) -> dict:
        return {**self._stats, "models_evaluated": len(self._reports)}


def build_jarvis_model_evaluator() -> ModelEvaluator:
    ev = ModelEvaluator()
    ev.set_tasks(
        [
            EvalTask(
                "eval-01",
                "What is the capital of France? Answer in one word.",
                expected_keywords=["Paris"],
            ),
            EvalTask(
                "eval-02",
                "List three programming languages in a comma-separated list.",
                expected_keywords=["Python", "Java", "C"],
            ),
            EvalTask(
                "eval-03",
                "Translate 'Hello' to Spanish.",
                expected_keywords=["Hola"],
            ),
            EvalTask(
                "eval-04",
                "What is 15 * 7? Answer with only the number.",
                expected_keywords=["105"],
            ),
            EvalTask(
                "eval-05",
                "In one sentence, explain what a neural network is.",
                expected_keywords=["layer", "neuron", "learn"],
                max_tokens=100,
            ),
        ]
    )
    return ev


async def main():
    import sys

    ev = build_jarvis_model_evaluator()
    await ev.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "compare"

    if cmd == "compare":
        models = [
            ("qwen3.5-9b", "http://192.168.1.85:1234"),
            ("qwen3.5-9b", "http://192.168.1.26:1234"),
        ]
        print(f"Evaluating {len(models)} models × {len(ev._tasks)} tasks...")
        reports = await ev.compare_models(models)

        print("\nLeaderboard:")
        for i, r in enumerate(reports):
            print(
                f"  {i + 1}. {r.model:<25} score={r.composite_score:.1f}/100 "
                f"avail={r.availability:.1%} lat={r.avg_latency_ms:.0f}ms "
                f"tps={r.avg_tokens_per_s:.1f} quality={r.avg_keyword_score:.2f}"
            )

        best = ev.best_model()
        if best:
            print(f"\nBest model: {best}")

        print(f"\nStats: {json.dumps(ev.stats(), indent=2)}")

    elif cmd == "leaderboard":
        for r in ev.leaderboard():
            print(json.dumps(r))

    elif cmd == "stats":
        print(json.dumps(ev.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
