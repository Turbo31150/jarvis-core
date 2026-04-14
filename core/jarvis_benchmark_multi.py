#!/usr/bin/env python3
"""
jarvis_benchmark_multi — Multi-model concurrent benchmark runner with SQL persistence
Benchmarks M1/M2/OL1 endpoints in parallel, stores results in SQLite,
generates comparison matrix and improvement suggestions.
"""

import asyncio
import json
import logging
import sqlite3
import statistics
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

log = logging.getLogger("jarvis.benchmark_multi")

DB_PATH = Path("/home/turbo/jarvis/data/etoile.db")


# ── Schema ────────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS benchmark_runs (
    run_id      TEXT NOT NULL,
    ts          TEXT NOT NULL DEFAULT (datetime('now')),
    model       TEXT NOT NULL,
    node        TEXT NOT NULL,
    prompt_tag  TEXT NOT NULL,
    concurrency INTEGER NOT NULL DEFAULT 1,
    iterations  INTEGER NOT NULL,
    p50_ms      REAL,
    p95_ms      REAL,
    p99_ms      REAL,
    mean_ms     REAL,
    stddev_ms   REAL,
    tps         REAL,
    error_rate  REAL,
    tokens_out  INTEGER,
    raw_json    TEXT,
    PRIMARY KEY (run_id, model, prompt_tag, concurrency)
);

CREATE TABLE IF NOT EXISTS benchmark_improvements (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    ts          TEXT NOT NULL DEFAULT (datetime('now')),
    model       TEXT NOT NULL,
    metric      TEXT NOT NULL,
    current_val REAL,
    baseline_val REAL,
    delta_pct   REAL,
    suggestion  TEXT
);
"""


@contextmanager
def _db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_DDL)
        conn.commit()
        yield conn
    finally:
        conn.close()


# ── Endpoint config ───────────────────────────────────────────────────────────


class NodeKind(str, Enum):
    LMSTUDIO = "lmstudio"
    OLLAMA = "ollama"


@dataclass
class Endpoint:
    name: str
    node: str
    url: str
    kind: NodeKind
    models: list[str] = field(default_factory=list)
    timeout_s: float = 45.0
    enabled: bool = True


ENDPOINTS = [
    Endpoint(
        "m1-lmstudio",
        "m1",
        "http://192.168.1.85:1234/v1/chat/completions",
        NodeKind.LMSTUDIO,
        ["qwen/qwen3.5-9b", "qwen/qwen3.5-35b-a3b", "deepseek/deepseek-r1"],
    ),
    Endpoint(
        "m2-lmstudio",
        "m2",
        "http://192.168.1.26:1234/v1/chat/completions",
        NodeKind.LMSTUDIO,
        ["qwen/qwen3.5-9b", "deepseek/deepseek-r1-0528"],
    ),
    Endpoint(
        "ol1-ollama",
        "ol1",
        "http://127.0.0.1:11434/api/chat",
        NodeKind.OLLAMA,
        ["gemma3:4b", "llama3.2"],
    ),
]

PROMPTS = {
    "short": "Say 'ok' in one word.",
    "medium": "List 5 GPU models in one sentence.",
    "reason": "What is 17 × 23? Show your work briefly.",
    "code": "Write a Python function: def add(a, b): that returns a+b. One line.",
}


# ── HTTP inference helper ─────────────────────────────────────────────────────


def _post_lmstudio(url: str, model: str, prompt: str, timeout: float) -> dict:
    payload = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 80,
            "temperature": 0,
            "stream": False,
        }
    ).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    tokens = data.get("usage", {}).get("completion_tokens", 0)
    return {"elapsed_ms": elapsed_ms, "tokens": tokens}


def _post_ollama(url: str, model: str, prompt: str, timeout: float) -> dict:
    payload = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "options": {"num_predict": 80},
        }
    ).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    tokens = data.get("eval_count", 0)
    return {"elapsed_ms": elapsed_ms, "tokens": tokens}


async def _call_one(endpoint: Endpoint, model: str, prompt: str) -> dict:
    loop = asyncio.get_event_loop()
    try:
        if endpoint.kind == NodeKind.LMSTUDIO:
            result = await loop.run_in_executor(
                None, _post_lmstudio, endpoint.url, model, prompt, endpoint.timeout_s
            )
        else:
            result = await loop.run_in_executor(
                None, _post_ollama, endpoint.url, model, prompt, endpoint.timeout_s
            )
        return {**result, "error": None}
    except Exception as e:
        return {"elapsed_ms": 0, "tokens": 0, "error": str(e)[:120]}


# ── Core benchmark logic ──────────────────────────────────────────────────────


@dataclass
class BenchResult:
    run_id: str
    model: str
    node: str
    prompt_tag: str
    concurrency: int
    iterations: int
    latencies: list[float] = field(default_factory=list)
    tokens_list: list[int] = field(default_factory=list)
    errors: int = 0
    total_s: float = 0.0

    @property
    def n(self) -> int:
        return len(self.latencies)

    @property
    def p50(self) -> float:
        return statistics.median(self.latencies) if self.latencies else 0.0

    @property
    def p95(self) -> float:
        if not self.latencies:
            return 0.0
        s = sorted(self.latencies)
        return s[min(int(len(s) * 0.95), len(s) - 1)]

    @property
    def p99(self) -> float:
        if not self.latencies:
            return 0.0
        s = sorted(self.latencies)
        return s[min(int(len(s) * 0.99), len(s) - 1)]

    @property
    def mean(self) -> float:
        return statistics.mean(self.latencies) if self.latencies else 0.0

    @property
    def stddev(self) -> float:
        return statistics.stdev(self.latencies) if len(self.latencies) > 1 else 0.0

    @property
    def tps(self) -> float:
        avg_tokens = sum(self.tokens_list) / max(len(self.tokens_list), 1)
        dur_s = self.mean / 1000
        return avg_tokens / max(dur_s, 0.001)

    @property
    def error_rate(self) -> float:
        total = self.n + self.errors
        return self.errors / max(total, 1)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "model": self.model,
            "node": self.node,
            "prompt_tag": self.prompt_tag,
            "concurrency": self.concurrency,
            "iterations": self.iterations,
            "p50_ms": round(self.p50, 1),
            "p95_ms": round(self.p95, 1),
            "p99_ms": round(self.p99, 1),
            "mean_ms": round(self.mean, 1),
            "stddev_ms": round(self.stddev, 1),
            "tps": round(self.tps, 2),
            "error_rate": round(self.error_rate, 4),
            "tokens_out": sum(self.tokens_list),
        }


async def _run_benchmark(
    endpoint: Endpoint,
    model: str,
    prompt_tag: str,
    prompt: str,
    run_id: str,
    iterations: int = 10,
    warmup: int = 2,
    concurrency: int = 1,
) -> BenchResult:
    result = BenchResult(
        run_id=run_id,
        model=model,
        node=endpoint.node,
        prompt_tag=prompt_tag,
        concurrency=concurrency,
        iterations=iterations,
    )

    # Warmup
    for _ in range(warmup):
        await _call_one(endpoint, model, prompt)

    t_start = time.perf_counter()

    # Concurrent batches
    calls_done = 0
    while calls_done < iterations:
        batch_size = min(concurrency, iterations - calls_done)
        tasks = [_call_one(endpoint, model, prompt) for _ in range(batch_size)]
        responses = await asyncio.gather(*tasks)
        for r in responses:
            if r["error"]:
                result.errors += 1
            else:
                result.latencies.append(r["elapsed_ms"])
                result.tokens_list.append(r["tokens"])
        calls_done += batch_size

    result.total_s = time.perf_counter() - t_start
    return result


# ── Improvement analysis ──────────────────────────────────────────────────────


def _analyze_improvements(results: list[BenchResult], run_id: str) -> list[dict]:
    suggestions = []

    # Compare models by p95 latency on same prompt
    by_prompt: dict[str, list[BenchResult]] = {}
    for r in results:
        by_prompt.setdefault(r.prompt_tag, []).append(r)

    for ptag, group in by_prompt.items():
        if len(group) < 2:
            continue
        best = min(group, key=lambda r: r.p95)
        for r in group:
            if r.model == best.model:
                continue
            if best.p95 == 0:
                continue
            delta_pct = (r.p95 - best.p95) / best.p95 * 100
            if delta_pct > 15:
                suggestions.append(
                    {
                        "run_id": run_id,
                        "model": r.model,
                        "metric": "p95_latency",
                        "current_val": round(r.p95, 1),
                        "baseline_val": round(best.p95, 1),
                        "delta_pct": round(delta_pct, 1),
                        "suggestion": (
                            f"Route '{ptag}' prompts to {best.model}@{best.node} "
                            f"({delta_pct:.0f}% faster p95)"
                        ),
                    }
                )

    # Flag high error rates
    for r in results:
        if r.error_rate > 0.1:
            suggestions.append(
                {
                    "run_id": run_id,
                    "model": r.model,
                    "metric": "error_rate",
                    "current_val": round(r.error_rate, 4),
                    "baseline_val": 0.0,
                    "delta_pct": r.error_rate * 100,
                    "suggestion": f"{r.model}@{r.node} has {r.error_rate:.0%} error rate — check endpoint health",
                }
            )

    # Flag high stddev (instability)
    for r in results:
        if r.mean > 0 and r.stddev / r.mean > 0.5:
            suggestions.append(
                {
                    "run_id": run_id,
                    "model": r.model,
                    "metric": "instability",
                    "current_val": round(r.stddev, 1),
                    "baseline_val": round(r.mean, 1),
                    "delta_pct": round(r.stddev / r.mean * 100, 1),
                    "suggestion": (
                        f"{r.model}@{r.node} is unstable (stddev={r.stddev:.0f}ms vs mean={r.mean:.0f}ms)"
                    ),
                }
            )

    return suggestions


# ── SQL persistence ───────────────────────────────────────────────────────────


def _save_results(results: list[BenchResult], suggestions: list[dict]):
    with _db() as conn:
        for r in results:
            d = r.to_dict()
            conn.execute(
                """INSERT OR REPLACE INTO benchmark_runs
                   (run_id, model, node, prompt_tag, concurrency, iterations,
                    p50_ms, p95_ms, p99_ms, mean_ms, stddev_ms, tps,
                    error_rate, tokens_out, raw_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    d["run_id"],
                    d["model"],
                    d["node"],
                    d["prompt_tag"],
                    d["concurrency"],
                    d["iterations"],
                    d["p50_ms"],
                    d["p95_ms"],
                    d["p99_ms"],
                    d["mean_ms"],
                    d["stddev_ms"],
                    d["tps"],
                    d["error_rate"],
                    d["tokens_out"],
                    json.dumps(d),
                ),
            )
        for s in suggestions:
            conn.execute(
                """INSERT INTO benchmark_improvements
                   (run_id, model, metric, current_val, baseline_val, delta_pct, suggestion)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    s["run_id"],
                    s["model"],
                    s["metric"],
                    s["current_val"],
                    s["baseline_val"],
                    s["delta_pct"],
                    s["suggestion"],
                ),
            )
        conn.commit()


# ── Main benchmark orchestrator ───────────────────────────────────────────────


class MultiBenchmark:
    def __init__(
        self,
        endpoints: list[Endpoint] | None = None,
        iterations: int = 10,
        warmup: int = 2,
        concurrencies: list[int] | None = None,
        prompt_tags: list[str] | None = None,
    ):
        self.endpoints = endpoints or ENDPOINTS
        self.iterations = iterations
        self.warmup = warmup
        self.concurrencies = concurrencies or [1, 5]
        self.prompt_tags = prompt_tags or list(PROMPTS.keys())

    async def run(self, run_id: str | None = None) -> dict:
        import secrets

        run_id = run_id or f"bench_{int(time.time())}_{secrets.token_hex(4)}"

        log.info(f"Starting multi-benchmark run_id={run_id}")
        t0 = time.perf_counter()

        # Build all tasks
        tasks = []
        for ep in self.endpoints:
            if not ep.enabled:
                continue
            for model in ep.models:
                for ptag in self.prompt_tags:
                    prompt = PROMPTS.get(ptag, "Say hi.")
                    for conc in self.concurrencies:
                        tasks.append((ep, model, ptag, prompt, conc))

        # Run all concurrently (batched to avoid overwhelming)
        batch_size = 4
        all_results: list[BenchResult] = []
        for i in range(0, len(tasks), batch_size):
            batch = tasks[i : i + batch_size]
            coros = [
                _run_benchmark(
                    ep, model, ptag, prompt, run_id, self.iterations, self.warmup, conc
                )
                for ep, model, ptag, prompt, conc in batch
            ]
            batch_results = await asyncio.gather(*coros, return_exceptions=True)
            for r in batch_results:
                if isinstance(r, BenchResult):
                    all_results.append(r)
                else:
                    log.error(f"Benchmark error: {r}")

        total_s = time.perf_counter() - t0
        suggestions = _analyze_improvements(all_results, run_id)
        _save_results(all_results, suggestions)

        summary = {
            "run_id": run_id,
            "total_benchmarks": len(all_results),
            "total_duration_s": round(total_s, 2),
            "improvements_found": len(suggestions),
            "results": [r.to_dict() for r in all_results],
            "suggestions": suggestions,
        }

        log.info(
            f"Benchmark complete: {len(all_results)} runs in {total_s:.1f}s, "
            f"{len(suggestions)} improvements found"
        )
        return summary

    def compare_matrix(self, run_id: str | None = None) -> list[dict]:
        """Query SQL for comparison matrix of latest or given run."""
        with _db() as conn:
            if run_id:
                rows = conn.execute(
                    "SELECT * FROM benchmark_runs WHERE run_id=? ORDER BY p95_ms",
                    (run_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM benchmark_runs
                       WHERE run_id=(SELECT run_id FROM benchmark_runs ORDER BY ts DESC LIMIT 1)
                       ORDER BY p95_ms"""
                ).fetchall()
            return [dict(r) for r in rows]

    def latest_suggestions(self, limit: int = 20) -> list[dict]:
        with _db() as conn:
            rows = conn.execute(
                "SELECT * FROM benchmark_improvements ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def history(self, model: str | None = None, days: int = 7) -> list[dict]:
        with _db() as conn:
            if model:
                rows = conn.execute(
                    """SELECT run_id, ts, model, node, prompt_tag, concurrency,
                              p50_ms, p95_ms, tps, error_rate
                       FROM benchmark_runs
                       WHERE model=? AND ts >= datetime('now', ?)
                       ORDER BY ts DESC""",
                    (model, f"-{days} days"),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT run_id, ts, model, node, prompt_tag, concurrency,
                              p50_ms, p95_ms, tps, error_rate
                       FROM benchmark_runs
                       WHERE ts >= datetime('now', ?)
                       ORDER BY ts DESC""",
                    (f"-{days} days",),
                ).fetchall()
            return [dict(r) for r in rows]


def build_jarvis_benchmark_multi(
    iterations: int = 8,
    warmup: int = 1,
    concurrencies: list[int] | None = None,
    prompt_tags: list[str] | None = None,
) -> MultiBenchmark:
    return MultiBenchmark(
        iterations=iterations,
        warmup=warmup,
        concurrencies=concurrencies or [1, 3],
        prompt_tags=prompt_tags or ["short", "medium"],
    )


async def main():
    import sys

    logging.basicConfig(level=logging.INFO)

    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"

    if cmd == "run":
        bench = build_jarvis_benchmark_multi(iterations=5, warmup=1)
        summary = await bench.run()

        print(f"\n{'=' * 60}")
        print(f"Run: {summary['run_id']}")
        print(
            f"Benchmarks: {summary['total_benchmarks']} in {summary['total_duration_s']}s"
        )
        print(f"{'=' * 60}\n")

        # Print comparison table
        print(
            f"{'Model':<25} {'Node':<5} {'Tag':<8} {'Conc':<5} {'p50':>7} {'p95':>7} {'TPS':>6} {'Err%':>5}"
        )
        print("-" * 70)
        for r in sorted(
            summary["results"], key=lambda x: (x["prompt_tag"], x["p95_ms"])
        ):
            print(
                f"{r['model']:<25} {r['node']:<5} {r['prompt_tag']:<8} "
                f"{r['concurrency']:<5} {r['p50_ms']:>7.0f} {r['p95_ms']:>7.0f} "
                f"{r['tps']:>6.1f} {r['error_rate'] * 100:>5.1f}"
            )

        if summary["suggestions"]:
            print(f"\n{'─' * 60}")
            print("Improvement suggestions:")
            for s in summary["suggestions"]:
                print(f"  ⚡ {s['suggestion']}")

    elif cmd == "matrix":
        bench = MultiBenchmark()
        rows = bench.compare_matrix()
        print(json.dumps(rows[:10], indent=2))

    elif cmd == "history":
        model = sys.argv[2] if len(sys.argv) > 2 else None
        bench = MultiBenchmark()
        rows = bench.history(model=model)
        print(json.dumps(rows[:20], indent=2))

    elif cmd == "suggestions":
        bench = MultiBenchmark()
        sug = bench.latest_suggestions(10)
        for s in sug:
            print(f"  [{s['metric']}] {s['suggestion']}")


if __name__ == "__main__":
    asyncio.run(main())
