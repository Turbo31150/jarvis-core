#!/usr/bin/env python3
"""
jarvis_domino_pipeline — Domino pipeline engine with multi-branch support
Fan-out / fan-in, conditional branches, SQL persistence in etoile.db
Extends existing domino_logs schema with branch tracking.
"""

import asyncio
import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("jarvis.domino_pipeline")

DB_PATH = Path("/home/turbo/jarvis/data/etoile.db")

# ── Schema extensions ─────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS domino_runs (
    run_id      TEXT PRIMARY KEY,
    pipeline_id TEXT NOT NULL,
    ts_start    TEXT NOT NULL DEFAULT (datetime('now')),
    ts_end      TEXT,
    status      TEXT NOT NULL DEFAULT 'RUNNING',
    total_steps INTEGER DEFAULT 0,
    passed      INTEGER DEFAULT 0,
    failed      INTEGER DEFAULT 0,
    skipped     INTEGER DEFAULT 0,
    duration_ms REAL,
    trigger     TEXT,
    context_json TEXT,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS domino_step_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    pipeline_id     TEXT NOT NULL,
    branch_id       TEXT NOT NULL DEFAULT 'main',
    step_name       TEXT NOT NULL,
    step_idx        INTEGER NOT NULL,
    parent_step     TEXT,
    status          TEXT NOT NULL,
    duration_ms     REAL,
    node            TEXT,
    output_preview  TEXT,
    output_json     TEXT,
    error           TEXT,
    ts              TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (run_id) REFERENCES domino_runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_domino_step_run ON domino_step_logs(run_id);
CREATE INDEX IF NOT EXISTS idx_domino_step_pipeline ON domino_step_logs(pipeline_id, ts);
"""


@contextmanager
def _db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        conn.executescript(_DDL)
        conn.commit()
        yield conn
    finally:
        conn.close()


# ── Step types ────────────────────────────────────────────────────────────────


class StepKind(str, Enum):
    SEQUENTIAL = "sequential"  # normal step
    BRANCH = "branch"  # fan-out to multiple parallel branches
    JOIN = "join"  # fan-in: wait for all / any branches
    CONDITION = "condition"  # run branch A or B based on predicate
    LOOP = "loop"  # repeat N times or while predicate
    LLM = "llm"  # LLM inference step
    BASH = "bash"  # shell command
    CUSTOM = "custom"  # callable


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"
    TIMEOUT = "TIMEOUT"


class JoinMode(str, Enum):
    ALL = "all"  # wait for all branches
    ANY = "any"  # succeed when any branch succeeds


# ── Step definition ───────────────────────────────────────────────────────────


@dataclass
class StepDef:
    name: str
    kind: StepKind = StepKind.SEQUENTIAL
    fn: Callable | None = None
    # BRANCH: list of branch pipelines (each is list[StepDef])
    branches: list[list["StepDef"]] = field(default_factory=list)
    branch_names: list[str] = field(default_factory=list)
    # JOIN
    join_mode: JoinMode = JoinMode.ALL
    # CONDITION
    condition: Callable | None = None  # fn(ctx) -> bool
    true_branch: list["StepDef"] = field(default_factory=list)
    false_branch: list["StepDef"] = field(default_factory=list)
    # LOOP
    loop_count: int = 0
    loop_condition: Callable | None = None
    loop_steps: list["StepDef"] = field(default_factory=list)
    # Config
    timeout_s: float = 60.0
    retry: int = 0
    skip_on_fail: bool = False
    node: str = "LOCAL"
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


# ── Step result ───────────────────────────────────────────────────────────────


@dataclass
class StepResult:
    step_name: str
    branch_id: str
    status: StepStatus
    output: Any = None
    error: str = ""
    duration_ms: float = 0.0
    sub_results: list["StepResult"] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "step": self.step_name,
            "branch": self.branch_id,
            "status": self.status.value,
            "output": str(self.output)[:200] if self.output else None,
            "error": self.error[:200],
            "duration_ms": round(self.duration_ms, 2),
            "sub_results": [s.to_dict() for s in self.sub_results],
        }


# ── Pipeline run context ──────────────────────────────────────────────────────


@dataclass
class RunContext:
    run_id: str
    pipeline_id: str
    data: dict[str, Any] = field(default_factory=dict)
    step_outputs: dict[str, Any] = field(default_factory=dict)  # step_name -> output
    branch_results: dict[str, list[StepResult]] = field(default_factory=dict)
    failed_steps: list[str] = field(default_factory=list)
    ts_start: float = field(default_factory=time.time)

    def set(self, key: str, value: Any):
        self.data[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def record_output(self, step_name: str, output: Any):
        self.step_outputs[step_name] = output


# ── SQL helpers ───────────────────────────────────────────────────────────────


def _insert_run(ctx: RunContext, total_steps: int, trigger: str = ""):
    with _db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO domino_runs
               (run_id, pipeline_id, status, total_steps, trigger, context_json)
               VALUES (?,?,?,?,?,?)""",
            (
                ctx.run_id,
                ctx.pipeline_id,
                "RUNNING",
                total_steps,
                trigger,
                json.dumps(ctx.data, default=str),
            ),
        )
        conn.commit()


def _update_run(
    ctx: RunContext,
    status: str,
    passed: int,
    failed: int,
    skipped: int,
    duration_ms: float,
    error: str = "",
):
    with _db() as conn:
        conn.execute(
            """UPDATE domino_runs
               SET ts_end=datetime('now'), status=?, passed=?, failed=?,
                   skipped=?, duration_ms=?, error=?
               WHERE run_id=?""",
            (status, passed, failed, skipped, round(duration_ms, 2), error, ctx.run_id),
        )
        conn.commit()


def _log_step(
    ctx: RunContext,
    step_name: str,
    step_idx: int,
    branch_id: str,
    status: StepStatus,
    duration_ms: float,
    output: Any,
    error: str = "",
    parent_step: str = "",
    node: str = "LOCAL",
):
    preview = str(output)[:200] if output else ""
    output_json = json.dumps(output, default=str) if output else None
    with _db() as conn:
        conn.execute(
            """INSERT INTO domino_step_logs
               (run_id, pipeline_id, branch_id, step_name, step_idx,
                parent_step, status, duration_ms, node, output_preview,
                output_json, error)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                ctx.run_id,
                ctx.pipeline_id,
                branch_id,
                step_name,
                step_idx,
                parent_step or None,
                status.value,
                round(duration_ms, 2),
                node,
                preview,
                output_json,
                error[:500] if error else None,
            ),
        )
        conn.commit()


# ── Step executor ─────────────────────────────────────────────────────────────


async def _exec_fn(fn: Callable, ctx: RunContext) -> Any:
    if asyncio.iscoroutinefunction(fn):
        return await fn(ctx)
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fn, ctx)


async def _execute_step(
    step: StepDef,
    ctx: RunContext,
    step_idx: int,
    branch_id: str = "main",
    parent_step: str = "",
) -> StepResult:
    t0 = time.perf_counter()
    log.debug(f"[{ctx.run_id}] step={step.name} branch={branch_id}")

    # ── CONDITION ─────────────────────────────────────────────────────────────
    if step.kind == StepKind.CONDITION:
        try:
            cond = await _exec_fn(step.condition, ctx) if step.condition else False
        except Exception as e:
            cond = False
            log.warning(f"Condition eval failed: {e}")

        chosen_branch = step.true_branch if cond else step.false_branch
        chosen_name = "true" if cond else "false"
        sub_results = await _execute_branch(
            chosen_branch,
            ctx,
            branch_id=f"{branch_id}:{step.name}:{chosen_name}",
            parent_step=step.name,
        )
        duration_ms = (time.perf_counter() - t0) * 1000
        all_ok = all(r.status == StepStatus.PASS for r in sub_results)
        status = StepStatus.PASS if all_ok else StepStatus.FAIL
        result = StepResult(
            step.name,
            branch_id,
            status,
            output={"chosen": chosen_name},
            duration_ms=duration_ms,
            sub_results=sub_results,
        )
        _log_step(
            ctx,
            step.name,
            step_idx,
            branch_id,
            status,
            duration_ms,
            {"branch": chosen_name},
            parent_step=parent_step,
            node=step.node,
        )
        return result

    # ── BRANCH (fan-out) ──────────────────────────────────────────────────────
    if step.kind == StepKind.BRANCH:
        branch_tasks = []
        for i, sub_steps in enumerate(step.branches):
            bname = step.branch_names[i] if i < len(step.branch_names) else f"b{i}"
            branch_tasks.append(
                _execute_branch(
                    sub_steps,
                    ctx,
                    branch_id=f"{branch_id}:{step.name}:{bname}",
                    parent_step=step.name,
                )
            )
        all_branch_results = await asyncio.gather(*branch_tasks, return_exceptions=True)
        duration_ms = (time.perf_counter() - t0) * 1000

        flat_subs = []
        for br in all_branch_results:
            if isinstance(br, list):
                flat_subs.extend(br)

        all_ok = all(r.status == StepStatus.PASS for r in flat_subs)
        status = StepStatus.PASS if all_ok else StepStatus.FAIL
        result = StepResult(
            step.name,
            branch_id,
            status,
            output={"branches": len(step.branches)},
            duration_ms=duration_ms,
            sub_results=flat_subs,
        )
        _log_step(
            ctx,
            step.name,
            step_idx,
            branch_id,
            status,
            duration_ms,
            {"branches": len(step.branches)},
            parent_step=parent_step,
            node=step.node,
        )
        return result

    # ── JOIN ──────────────────────────────────────────────────────────────────
    if step.kind == StepKind.JOIN:
        # Evaluate join: check stored branch results
        duration_ms = (time.perf_counter() - t0) * 1000
        branch_data = ctx.branch_results.get(parent_step, [])
        if step.join_mode == JoinMode.ANY:
            ok = any(r.status == StepStatus.PASS for r in branch_data)
        else:
            ok = all(r.status == StepStatus.PASS for r in branch_data)
        status = StepStatus.PASS if ok else StepStatus.FAIL
        result = StepResult(step.name, branch_id, status, duration_ms=duration_ms)
        _log_step(
            ctx,
            step.name,
            step_idx,
            branch_id,
            status,
            duration_ms,
            {"join_mode": step.join_mode.value},
            parent_step=parent_step,
            node=step.node,
        )
        return result

    # ── LOOP ──────────────────────────────────────────────────────────────────
    if step.kind == StepKind.LOOP:
        iteration = 0
        max_iter = step.loop_count if step.loop_count > 0 else 100
        loop_results = []
        while iteration < max_iter:
            if step.loop_condition:
                try:
                    should_continue = await _exec_fn(step.loop_condition, ctx)
                    if not should_continue:
                        break
                except Exception:
                    break
            sub = await _execute_branch(
                step.loop_steps,
                ctx,
                branch_id=f"{branch_id}:{step.name}:iter{iteration}",
                parent_step=step.name,
            )
            loop_results.extend(sub)
            if any(r.status == StepStatus.FAIL for r in sub):
                break
            iteration += 1

        duration_ms = (time.perf_counter() - t0) * 1000
        all_ok = all(r.status == StepStatus.PASS for r in loop_results)
        status = StepStatus.PASS if all_ok else StepStatus.FAIL
        result = StepResult(
            step.name,
            branch_id,
            status,
            output={"iterations": iteration},
            duration_ms=duration_ms,
            sub_results=loop_results,
        )
        _log_step(
            ctx,
            step.name,
            step_idx,
            branch_id,
            status,
            duration_ms,
            {"iterations": iteration},
            parent_step=parent_step,
            node=step.node,
        )
        return result

    # ── SEQUENTIAL / LLM / BASH / CUSTOM ──────────────────────────────────────
    last_error = ""
    output = None
    attempts = step.retry + 1

    for attempt in range(attempts):
        try:
            coro = asyncio.wait_for(
                _exec_fn(step.fn, ctx) if step.fn else asyncio.sleep(0),
                timeout=step.timeout_s,
            )
            output = await coro
            ctx.record_output(step.name, output)
            duration_ms = (time.perf_counter() - t0) * 1000
            status = StepStatus.PASS
            _log_step(
                ctx,
                step.name,
                step_idx,
                branch_id,
                status,
                duration_ms,
                output,
                parent_step=parent_step,
                node=step.node,
            )
            return StepResult(
                step.name, branch_id, status, output=output, duration_ms=duration_ms
            )

        except asyncio.TimeoutError:
            last_error = f"timeout after {step.timeout_s}s"
            log.warning(f"Step {step.name} timeout (attempt {attempt + 1})")
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            log.warning(f"Step {step.name} error (attempt {attempt + 1}): {e}")
            if attempt < step.retry:
                await asyncio.sleep(0.5 * (attempt + 1))

    duration_ms = (time.perf_counter() - t0) * 1000
    status = StepStatus.SKIP if step.skip_on_fail else StepStatus.FAIL
    ctx.failed_steps.append(step.name)
    _log_step(
        ctx,
        step.name,
        step_idx,
        branch_id,
        status,
        duration_ms,
        None,
        error=last_error,
        parent_step=parent_step,
        node=step.node,
    )
    return StepResult(
        step.name, branch_id, status, error=last_error, duration_ms=duration_ms
    )


async def _execute_branch(
    steps: list[StepDef],
    ctx: RunContext,
    branch_id: str = "main",
    parent_step: str = "",
) -> list[StepResult]:
    results: list[StepResult] = []
    for idx, step in enumerate(steps):
        result = await _execute_step(step, ctx, idx, branch_id, parent_step)
        results.append(result)
        if result.status == StepStatus.FAIL and not step.skip_on_fail:
            # Abort this branch
            for remaining in steps[idx + 1 :]:
                results.append(StepResult(remaining.name, branch_id, StepStatus.SKIP))
            break
    return results


# ── Pipeline ──────────────────────────────────────────────────────────────────


@dataclass
class DominoPipeline:
    pipeline_id: str
    steps: list[StepDef]
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    async def run(
        self,
        initial_data: dict | None = None,
        trigger: str = "manual",
    ) -> "PipelineResult":
        import secrets

        run_id = f"{self.pipeline_id}_{int(time.time())}_{secrets.token_hex(4)}"
        ctx = RunContext(
            run_id=run_id,
            pipeline_id=self.pipeline_id,
            data=initial_data or {},
        )
        _insert_run(ctx, len(self.steps), trigger)

        t0 = time.perf_counter()
        results = await _execute_branch(self.steps, ctx)
        duration_ms = (time.perf_counter() - t0) * 1000

        passed = sum(1 for r in results if r.status == StepStatus.PASS)
        failed = sum(1 for r in results if r.status == StepStatus.FAIL)
        skipped = sum(1 for r in results if r.status == StepStatus.SKIP)
        pipeline_status = "PASS" if failed == 0 else "FAIL"

        _update_run(ctx, pipeline_status, passed, failed, skipped, duration_ms)

        return PipelineResult(
            run_id=run_id,
            pipeline_id=self.pipeline_id,
            status=pipeline_status,
            step_results=results,
            duration_ms=duration_ms,
            context=ctx,
        )


@dataclass
class PipelineResult:
    run_id: str
    pipeline_id: str
    status: str
    step_results: list[StepResult]
    duration_ms: float
    context: RunContext

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "pipeline_id": self.pipeline_id,
            "status": self.status,
            "duration_ms": round(self.duration_ms, 2),
            "steps": [r.to_dict() for r in self.step_results],
            "outputs": {k: str(v)[:100] for k, v in self.context.step_outputs.items()},
        }


# ── Pipeline registry ─────────────────────────────────────────────────────────


class DominoPipelineRegistry:
    def __init__(self):
        self._pipelines: dict[str, DominoPipeline] = {}
        self._stats: dict[str, int] = {"runs": 0, "pass": 0, "fail": 0}

    def register(self, pipeline: DominoPipeline):
        self._pipelines[pipeline.pipeline_id] = pipeline

    def get(self, pipeline_id: str) -> DominoPipeline | None:
        return self._pipelines.get(pipeline_id)

    async def run(self, pipeline_id: str, **kwargs) -> PipelineResult | None:
        pipeline = self._pipelines.get(pipeline_id)
        if not pipeline:
            log.error(f"Pipeline not found: {pipeline_id}")
            return None
        self._stats["runs"] += 1
        result = await pipeline.run(**kwargs)
        if result.status == "PASS":
            self._stats["pass"] += 1
        else:
            self._stats["fail"] += 1
        return result

    def list_pipelines(self) -> list[str]:
        return list(self._pipelines.keys())

    def recent_runs(
        self, pipeline_id: str | None = None, limit: int = 20
    ) -> list[dict]:
        with _db() as conn:
            if pipeline_id:
                rows = conn.execute(
                    """SELECT * FROM domino_runs WHERE pipeline_id=?
                       ORDER BY ts_start DESC LIMIT ?""",
                    (pipeline_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM domino_runs ORDER BY ts_start DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]

    def step_logs(self, run_id: str) -> list[dict]:
        with _db() as conn:
            rows = conn.execute(
                "SELECT * FROM domino_step_logs WHERE run_id=? ORDER BY id",
                (run_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def stats(self) -> dict:
        return {**self._stats, "pipelines": len(self._pipelines)}


# ── Factory: built-in pipelines ───────────────────────────────────────────────


def _make_cluster_health_pipeline() -> DominoPipeline:
    """Multi-branch cluster health check: probe all nodes in parallel."""
    import urllib.request

    async def probe_m1(ctx: RunContext):
        try:
            req = urllib.request.Request("http://192.168.1.85:1234/v1/models")
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.load(r)
            ctx.set("m1_models", len(data.get("data", [])))
            return {"node": "m1", "status": "up", "models": len(data.get("data", []))}
        except Exception as e:
            return {"node": "m1", "status": "down", "error": str(e)}

    async def probe_m2(ctx: RunContext):
        try:
            req = urllib.request.Request("http://192.168.1.26:1234/v1/models")
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.load(r)
            ctx.set("m2_models", len(data.get("data", [])))
            return {"node": "m2", "status": "up", "models": len(data.get("data", []))}
        except Exception as e:
            return {"node": "m2", "status": "down", "error": str(e)}

    async def probe_ol1(ctx: RunContext):
        try:
            req = urllib.request.Request("http://127.0.0.1:11434/api/tags")
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.load(r)
            models = [m["name"] for m in data.get("models", [])]
            ctx.set("ol1_models", models)
            return {"node": "ol1", "status": "up", "models": models}
        except Exception as e:
            return {"node": "ol1", "status": "down", "error": str(e)}

    async def aggregate(ctx: RunContext):
        m1 = ctx.get("m1_models", 0)
        m2 = ctx.get("m2_models", 0)
        ol1 = ctx.get("ol1_models", [])
        summary = {
            "m1_models": m1,
            "m2_models": m2,
            "ol1_models": ol1,
            "nodes_up": sum([bool(m1), bool(m2), bool(ol1)]),
        }
        ctx.set("health_summary", summary)
        return summary

    return DominoPipeline(
        pipeline_id="cluster_health_multi",
        description="Probe all cluster nodes in parallel, aggregate results",
        steps=[
            StepDef(
                name="probe_all_nodes",
                kind=StepKind.BRANCH,
                branch_names=["m1", "m2", "ol1"],
                branches=[
                    [
                        StepDef(
                            "probe_m1",
                            fn=probe_m1,
                            timeout_s=8,
                            skip_on_fail=True,
                            node="m1",
                        )
                    ],
                    [
                        StepDef(
                            "probe_m2",
                            fn=probe_m2,
                            timeout_s=8,
                            skip_on_fail=True,
                            node="m2",
                        )
                    ],
                    [
                        StepDef(
                            "probe_ol1",
                            fn=probe_ol1,
                            timeout_s=8,
                            skip_on_fail=True,
                            node="ol1",
                        )
                    ],
                ],
            ),
            StepDef("aggregate_health", fn=aggregate, node="LOCAL"),
        ],
    )


def _make_benchmark_pipeline() -> DominoPipeline:
    """Benchmark pipeline: short test on M1 and M2 in parallel, then compare."""
    import urllib.request

    async def bench_m1(ctx: RunContext):
        t0 = time.perf_counter()
        try:
            payload = json.dumps(
                {
                    "model": "qwen/qwen3.5-9b",
                    "messages": [{"role": "user", "content": "Say ok."}],
                    "max_tokens": 5,
                    "temperature": 0,
                    "stream": False,
                }
            ).encode()
            req = urllib.request.Request(
                "http://192.168.1.85:1234/v1/chat/completions",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
            ms = (time.perf_counter() - t0) * 1000
            ctx.set("m1_latency_ms", ms)
            return {"node": "m1", "latency_ms": round(ms, 1)}
        except Exception as e:
            return {"node": "m1", "error": str(e)}

    async def bench_m2(ctx: RunContext):
        t0 = time.perf_counter()
        try:
            payload = json.dumps(
                {
                    "model": "qwen/qwen3.5-9b",
                    "messages": [{"role": "user", "content": "Say ok."}],
                    "max_tokens": 5,
                    "temperature": 0,
                    "stream": False,
                }
            ).encode()
            req = urllib.request.Request(
                "http://192.168.1.26:1234/v1/chat/completions",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
            ms = (time.perf_counter() - t0) * 1000
            ctx.set("m2_latency_ms", ms)
            return {"node": "m2", "latency_ms": round(ms, 1)}
        except Exception as e:
            return {"node": "m2", "error": str(e)}

    async def compare(ctx: RunContext):
        m1 = ctx.get("m1_latency_ms", 0)
        m2 = ctx.get("m2_latency_ms", 0)
        winner = "m1" if (m1 and m1 <= m2) or not m2 else "m2"
        result = {"m1_ms": round(m1, 1), "m2_ms": round(m2, 1), "winner": winner}
        ctx.set("bench_winner", winner)
        return result

    async def save_bench(ctx: RunContext):

        run_id = f"quickbench_{int(time.time())}"
        m1 = ctx.get("m1_latency_ms", 0)
        m2 = ctx.get("m2_latency_ms", 0)
        with _db() as conn:
            for node, ms in [("m1", m1), ("m2", m2)]:
                if ms:
                    conn.execute(
                        """INSERT OR REPLACE INTO benchmark_runs
                           (run_id, model, node, prompt_tag, concurrency,
                            iterations, p50_ms, mean_ms, error_rate, tokens_out)
                           VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (
                            run_id,
                            "qwen/qwen3.5-9b",
                            node,
                            "quick",
                            1,
                            1,
                            round(ms, 1),
                            round(ms, 1),
                            0.0,
                            5,
                        ),
                    )
            conn.commit()
        return {"saved": True, "run_id": run_id}

    return DominoPipeline(
        pipeline_id="benchmark_multi_node",
        description="Quick benchmark M1+M2 in parallel, compare, save to SQL",
        steps=[
            StepDef(
                name="bench_parallel",
                kind=StepKind.BRANCH,
                branch_names=["m1", "m2"],
                branches=[
                    [StepDef("bench_m1", fn=bench_m1, timeout_s=35, skip_on_fail=True)],
                    [StepDef("bench_m2", fn=bench_m2, timeout_s=35, skip_on_fail=True)],
                ],
            ),
            StepDef("compare_results", fn=compare),
            StepDef("save_to_sql", fn=save_bench),
        ],
    )


def _make_conditional_pipeline() -> DominoPipeline:
    """Conditional pipeline: heavy model if complex query, fast model otherwise."""

    async def classify(ctx: RunContext):
        prompt = ctx.get("prompt", "")
        is_complex = len(prompt.split()) > 20
        ctx.set("is_complex", is_complex)
        return {"complex": is_complex}

    async def fast_infer(ctx: RunContext):
        ctx.set("model_used", "qwen3.5-9b")
        return {"model": "qwen3.5-9b", "note": "fast path"}

    async def deep_infer(ctx: RunContext):
        ctx.set("model_used", "deepseek-r1")
        return {"model": "deepseek-r1", "note": "reasoning path"}

    async def format_output(ctx: RunContext):
        return {
            "model_used": ctx.get("model_used", "unknown"),
            "prompt_preview": ctx.get("prompt", "")[:50],
        }

    return DominoPipeline(
        pipeline_id="conditional_routing",
        description="Route to fast or deep model based on prompt complexity",
        steps=[
            StepDef("classify_prompt", fn=classify),
            StepDef(
                name="route_model",
                kind=StepKind.CONDITION,
                condition=lambda ctx: ctx.get("is_complex", False),
                true_branch=[StepDef("deep_infer", fn=deep_infer)],
                false_branch=[StepDef("fast_infer", fn=fast_infer)],
            ),
            StepDef("format_output", fn=format_output),
        ],
    )


def build_jarvis_domino_pipeline() -> DominoPipelineRegistry:
    registry = DominoPipelineRegistry()
    registry.register(_make_cluster_health_pipeline())
    registry.register(_make_benchmark_pipeline())
    registry.register(_make_conditional_pipeline())
    return registry


async def main():
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    registry = build_jarvis_domino_pipeline()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Domino pipeline demo\n")

        # 1. Cluster health (multi-branch)
        print("── Cluster health (parallel branches) ──")
        r = await registry.run("cluster_health_multi")
        if r:
            print(f"  run_id: {r.run_id}")
            print(f"  status: {r.status}  ({r.duration_ms:.0f}ms)")
            for sr in r.step_results:
                _print_result(sr, indent=2)
        print()

        # 2. Benchmark (parallel + SQL save)
        print("── Benchmark multi-node (parallel + SQL) ──")
        r2 = await registry.run("benchmark_multi_node")
        if r2:
            print(
                f"  run_id: {r2.run_id}  status: {r2.status}  ({r2.duration_ms:.0f}ms)"
            )
            for sr in r2.step_results:
                _print_result(sr, indent=2)
            winner = r2.context.get("bench_winner")
            if winner:
                print(f"\n  → Winner: {winner}")
        print()

        # 3. Conditional routing (simple prompt → fast model)
        print("── Conditional routing (short prompt → fast model) ──")
        r3 = await registry.run(
            "conditional_routing", initial_data={"prompt": "Say hi"}
        )
        if r3:
            print(f"  status: {r3.status}  model_used={r3.context.get('model_used')}")
        print()

        # 4. Conditional routing (long prompt → deep model)
        print("── Conditional routing (long prompt → deep model) ──")
        r4 = await registry.run(
            "conditional_routing",
            initial_data={
                "prompt": "Analyze the cluster performance and explain "
                "why GPU utilization varies between nodes "
                "during concurrent inference workloads."
            },
        )
        if r4:
            print(f"  status: {r4.status}  model_used={r4.context.get('model_used')}")
        print()

        # Show recent SQL runs
        print("── Recent runs (SQL) ──")
        for row in registry.recent_runs(limit=5):
            print(
                f"  {row['run_id']:<45} {row['status']:<5} "
                f"{row.get('duration_ms') or 0:.0f}ms"
            )

        print(f"\nRegistry stats: {json.dumps(registry.stats(), indent=2)}")

    elif cmd == "run":
        pipeline_id = sys.argv[2] if len(sys.argv) > 2 else "cluster_health_multi"
        r = await registry.run(pipeline_id)
        if r:
            print(json.dumps(r.to_dict(), indent=2))

    elif cmd == "logs":
        run_id = sys.argv[2] if len(sys.argv) > 2 else ""
        if not run_id:
            runs = registry.recent_runs(limit=1)
            run_id = runs[0]["run_id"] if runs else ""
        if run_id:
            logs = registry.step_logs(run_id)
            for log_row in logs:
                status_icon = "✅" if log_row["status"] == "PASS" else "❌"
                print(
                    f"  {status_icon} [{log_row['branch_id']:<30}] "
                    f"{log_row['step_name']:<25} "
                    f"{log_row.get('duration_ms') or 0:.1f}ms"
                )

    elif cmd == "list":
        print("\n".join(registry.list_pipelines()))


def _print_result(r: StepResult, indent: int = 0):
    pad = " " * indent
    icon = (
        "✅"
        if r.status == StepStatus.PASS
        else ("⏭" if r.status == StepStatus.SKIP else "❌")
    )
    print(
        f"{pad}{icon} [{r.branch_id:<30}] {r.step_name:<25} {r.duration_ms:.0f}ms",
        f"→ {str(r.output)[:60]}" if r.output else "",
    )
    for sub in r.sub_results:
        _print_result(sub, indent + 4)


if __name__ == "__main__":
    asyncio.run(main())
