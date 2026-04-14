#!/usr/bin/env python3
"""
jarvis_model_benchmarker — Automated LLM benchmark runner
Tests models on standard prompts, measures throughput/latency/quality, generates reports
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.model_benchmarker")

BENCH_DIR = Path("/home/turbo/IA/Core/jarvis/data/benchmarks")
REDIS_PREFIX = "jarvis:bench:"
NODES = {"M1": "http://127.0.0.1:1234", "M2": "http://192.168.1.26:1234"}

STANDARD_PROMPTS = [
    {"id": "short_qa", "prompt": "What is Redis?", "expected_tokens": 80},
    {
        "id": "code_gen",
        "prompt": "Write a Python function to check if a number is prime.",
        "expected_tokens": 150,
    },
    {
        "id": "reasoning",
        "prompt": "If a train leaves at 9am going 80km/h and another at 10am going 100km/h in the same direction, when does the second catch up?",
        "expected_tokens": 120,
    },
    {
        "id": "summarize",
        "prompt": "Summarize in one sentence: Redis is an open-source, in-memory data structure store used as a database, cache, and message broker.",
        "expected_tokens": 40,
    },
    {
        "id": "long_gen",
        "prompt": "List 10 best practices for async Python programming.",
        "expected_tokens": 300,
    },
]


@dataclass
class BenchResult:
    run_id: str
    model: str
    node: str
    prompt_id: str
    prompt: str
    response: str
    tokens_out: int
    time_to_first_token_ms: float
    total_latency_ms: float
    tokens_per_second: float
    success: bool
    error: str = ""
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "model": self.model,
            "node": self.node,
            "prompt_id": self.prompt_id,
            "response_preview": self.response[:100],
            "tokens_out": self.tokens_out,
            "ttft_ms": round(self.time_to_first_token_ms, 1),
            "latency_ms": round(self.total_latency_ms, 1),
            "tok_per_s": round(self.tokens_per_second, 1),
            "success": self.success,
            "error": self.error,
            "ts": self.ts,
        }


@dataclass
class BenchReport:
    report_id: str
    model: str
    node: str
    results: list[BenchResult]
    started_at: float
    ended_at: float = 0.0

    @property
    def duration_s(self) -> float:
        return round((self.ended_at or time.time()) - self.started_at, 1)

    @property
    def success_rate(self) -> float:
        if not self.results:
            return 0.0
        return sum(1 for r in self.results if r.success) / len(self.results)

    def summary(self) -> dict:
        ok = [r for r in self.results if r.success]
        if not ok:
            return {"model": self.model, "node": self.node, "success_rate": 0.0}
        latencies = [r.total_latency_ms for r in ok]
        tps_list = [r.tokens_per_second for r in ok]
        return {
            "report_id": self.report_id,
            "model": self.model,
            "node": self.node,
            "duration_s": self.duration_s,
            "total_prompts": len(self.results),
            "success_rate": round(self.success_rate * 100, 1),
            "avg_latency_ms": round(mean(latencies), 1),
            "p50_latency_ms": round(sorted(latencies)[len(latencies) // 2], 1),
            "p95_latency_ms": round(sorted(latencies)[int(len(latencies) * 0.95)], 1),
            "avg_tok_per_s": round(mean(tps_list), 1),
            "max_tok_per_s": round(max(tps_list), 1),
        }


class ModelBenchmarker:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._reports: list[BenchReport] = []
        BENCH_DIR.mkdir(parents=True, exist_ok=True)

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    async def _run_single(
        self,
        model: str,
        node_url: str,
        node: str,
        prompt_item: dict,
        max_tokens: int = 512,
    ) -> BenchResult:
        run_id = str(uuid.uuid4())[:6]
        prompt = prompt_item["prompt"]
        t_start = time.time()
        t_first_token = 0.0
        response_text = ""
        tokens_out = 0
        error = ""

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=90)
            ) as sess:
                async with sess.post(
                    f"{node_url}/v1/chat/completions",
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": max_tokens,
                        "temperature": 0.1,
                        "stream": True,
                    },
                ) as r:
                    if r.status != 200:
                        raise RuntimeError(f"HTTP {r.status}")
                    async for line_bytes in r.content:
                        line = line_bytes.decode().strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                            token = (
                                chunk["choices"][0].get("delta", {}).get("content", "")
                            )
                            if token:
                                if not t_first_token:
                                    t_first_token = time.time()
                                response_text += token
                                tokens_out += 1
                        except Exception:
                            pass

        except Exception as e:
            error = str(e)[:100]

        t_end = time.time()
        total_ms = (t_end - t_start) * 1000
        ttft_ms = (t_first_token - t_start) * 1000 if t_first_token else total_ms
        tps = tokens_out / max((t_end - t_start), 0.001)

        return BenchResult(
            run_id=run_id,
            model=model,
            node=node,
            prompt_id=prompt_item["id"],
            prompt=prompt,
            response=response_text,
            tokens_out=tokens_out,
            time_to_first_token_ms=ttft_ms,
            total_latency_ms=total_ms,
            tokens_per_second=tps,
            success=bool(response_text and not error),
            error=error,
        )

    async def run_benchmark(
        self,
        model: str,
        node: str = "M1",
        prompts: list[dict] | None = None,
        concurrency: int = 1,
        warmup: bool = True,
    ) -> BenchReport:
        node_url = NODES.get(node, NODES["M1"])
        prompts = prompts or STANDARD_PROMPTS
        report = BenchReport(
            report_id=str(uuid.uuid4())[:8],
            model=model,
            node=node,
            results=[],
            started_at=time.time(),
        )

        # Warmup
        if warmup:
            log.info(f"Warmup: {model} on {node}")
            await self._run_single(
                model, node_url, node, {"id": "warmup", "prompt": "Hi"}, 10
            )

        log.info(
            f"Benchmarking {model} on {node} ({len(prompts)} prompts, concurrency={concurrency})"
        )
        sem = asyncio.Semaphore(concurrency)

        async def bounded(p):
            async with sem:
                return await self._run_single(model, node_url, node, p)

        results = await asyncio.gather(*[bounded(p) for p in prompts])
        report.results = list(results)
        report.ended_at = time.time()

        self._save_report(report)
        self._reports.append(report)

        if self.redis:
            summary = report.summary()
            await self.redis.lpush(f"{REDIS_PREFIX}reports", json.dumps(summary))
            await self.redis.ltrim(f"{REDIS_PREFIX}reports", 0, 999)
            await self.redis.hset(
                f"{REDIS_PREFIX}latest",
                f"{node}:{model}",
                json.dumps(summary),
            )

        log.info(
            f"Benchmark done: {model} avg={report.summary().get('avg_tok_per_s', 0):.1f} tok/s "
            f"success={report.success_rate:.0%}"
        )
        return report

    def _save_report(self, report: BenchReport):
        ts = time.strftime("%Y%m%d_%H%M%S", time.localtime(report.started_at))
        path = (
            BENCH_DIR
            / f"bench_{report.node}_{report.model.replace('/', '_')}_{ts}.json"
        )
        path.write_text(
            json.dumps(
                {
                    "summary": report.summary(),
                    "results": [r.to_dict() for r in report.results],
                },
                indent=2,
            )
        )

    async def compare_models(self, models: list[str], node: str = "M1") -> list[dict]:
        summaries = []
        for model in models:
            report = await self.run_benchmark(model, node)
            summaries.append(report.summary())
        return sorted(summaries, key=lambda x: -x.get("avg_tok_per_s", 0))

    def list_reports(self) -> list[dict]:
        return [r.summary() for r in sorted(self._reports, key=lambda x: -x.started_at)]


async def main():
    import sys

    bencher = ModelBenchmarker()
    await bencher.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo" or cmd == "run":
        model = sys.argv[2] if len(sys.argv) > 2 else "qwen/qwen3.5-9b"
        node = sys.argv[3] if len(sys.argv) > 3 else "M1"
        report = await bencher.run_benchmark(model, node, concurrency=2)
        s = report.summary()
        print(f"\nModel: {model} | Node: {node}")
        print(f"Success: {s['success_rate']}% | Avg latency: {s['avg_latency_ms']}ms")
        print(f"Throughput: {s['avg_tok_per_s']} tok/s (max {s['max_tok_per_s']})")
        print(f"P50: {s['p50_latency_ms']}ms | P95: {s['p95_latency_ms']}ms")
        print("\nPer-prompt:")
        for r in report.results:
            ok = "✅" if r.success else "❌"
            print(
                f"  {ok} [{r.prompt_id}] {r.tokens_out}t {r.total_latency_ms:.0f}ms {r.tokens_per_second:.1f}tok/s"
            )

    elif cmd == "compare" and len(sys.argv) > 2:
        models = sys.argv[2:]
        results = await bencher.compare_models(models)
        print(f"\n{'Model':<40} {'tok/s':>8} {'p50ms':>8} {'success':>8}")
        print("-" * 70)
        for s in results:
            print(
                f"  {s['model']:<40} {s['avg_tok_per_s']:>8.1f} {s['p50_latency_ms']:>8.0f} {s['success_rate']:>7.0f}%"
            )


if __name__ == "__main__":
    asyncio.run(main())
