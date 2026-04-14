#!/usr/bin/env python3
"""
jarvis_experiment_tracker — ML experiment tracking and comparison
Tracks runs, hyperparams, metrics, artifacts; compare across experiments
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.experiment_tracker")

EXPERIMENTS_DIR = Path("/home/turbo/IA/Core/jarvis/data/experiments")
REDIS_PREFIX = "jarvis:exp:"


@dataclass
class RunMetrics:
    step: int
    metrics: dict[str, float]
    ts: float = field(default_factory=time.time)


@dataclass
class ExperimentRun:
    run_id: str
    experiment_name: str
    params: dict[str, Any]
    status: str = "running"  # running | completed | failed
    metrics_history: list[RunMetrics] = field(default_factory=list)
    final_metrics: dict[str, float] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)  # name→path
    tags: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    ended_at: float = 0.0
    notes: str = ""

    @property
    def duration_s(self) -> float:
        end = self.ended_at or time.time()
        return round(end - self.started_at, 1)

    def best_metric(self, name: str, higher_is_better: bool = True) -> float | None:
        values = [
            r.metrics.get(name) for r in self.metrics_history if name in r.metrics
        ]
        if not values:
            return self.final_metrics.get(name)
        return max(values) if higher_is_better else min(values)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "experiment_name": self.experiment_name,
            "params": self.params,
            "status": self.status,
            "final_metrics": self.final_metrics,
            "artifacts": self.artifacts,
            "tags": self.tags,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_s": self.duration_s,
            "notes": self.notes,
            "steps": len(self.metrics_history),
        }

    def to_full_dict(self) -> dict:
        d = self.to_dict()
        d["metrics_history"] = [
            {"step": r.step, "metrics": r.metrics, "ts": r.ts}
            for r in self.metrics_history[-100:]  # last 100 steps
        ]
        return d


class ExperimentTracker:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._runs: dict[str, ExperimentRun] = {}
        EXPERIMENTS_DIR.mkdir(parents=True, exist_ok=True)

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def start_run(
        self,
        experiment_name: str,
        params: dict | None = None,
        tags: list[str] | None = None,
        notes: str = "",
    ) -> ExperimentRun:
        run = ExperimentRun(
            run_id=str(uuid.uuid4())[:8],
            experiment_name=experiment_name,
            params=params or {},
            tags=tags or [],
            notes=notes,
        )
        self._runs[run.run_id] = run
        log.info(f"Started run [{run.run_id}] experiment={experiment_name}")
        return run

    async def log_metrics(
        self,
        run_id: str,
        metrics: dict[str, float],
        step: int | None = None,
    ):
        run = self._runs.get(run_id)
        if not run:
            log.warning(f"Run '{run_id}' not found")
            return
        if step is None:
            step = len(run.metrics_history)
        run.metrics_history.append(RunMetrics(step=step, metrics=metrics))
        # Update final metrics
        run.final_metrics.update(metrics)

        if self.redis:
            await self.redis.hset(
                f"{REDIS_PREFIX}run:{run_id}:metrics",
                step,
                json.dumps(metrics),
            )

    def log_param(self, run_id: str, key: str, value: Any):
        run = self._runs.get(run_id)
        if run:
            run.params[key] = value

    def log_artifact(self, run_id: str, name: str, path: str):
        run = self._runs.get(run_id)
        if run:
            run.artifacts[name] = path
            log.debug(f"Artifact [{run_id}] {name} → {path}")

    async def end_run(
        self,
        run_id: str,
        status: str = "completed",
        final_metrics: dict | None = None,
    ):
        run = self._runs.get(run_id)
        if not run:
            return
        run.status = status
        run.ended_at = time.time()
        if final_metrics:
            run.final_metrics.update(final_metrics)

        self._save_run(run)
        if self.redis:
            await self.redis.set(
                f"{REDIS_PREFIX}run:{run_id}",
                json.dumps(run.to_dict()),
                ex=86400 * 30,
            )
            await self.redis.sadd(
                f"{REDIS_PREFIX}exp:{run.experiment_name}:runs", run_id
            )
        log.info(
            f"Run [{run_id}] {status} in {run.duration_s}s | metrics={run.final_metrics}"
        )

    def _save_run(self, run: ExperimentRun):
        exp_dir = EXPERIMENTS_DIR / run.experiment_name
        exp_dir.mkdir(parents=True, exist_ok=True)
        run_file = exp_dir / f"{run.run_id}.json"
        run_file.write_text(json.dumps(run.to_full_dict(), indent=2))

    def get_run(self, run_id: str) -> ExperimentRun | None:
        return self._runs.get(run_id)

    def list_runs(
        self, experiment_name: str | None = None, status: str | None = None
    ) -> list[dict]:
        runs = list(self._runs.values())
        if experiment_name:
            runs = [r for r in runs if r.experiment_name == experiment_name]
        if status:
            runs = [r for r in runs if r.status == status]
        return [r.to_dict() for r in sorted(runs, key=lambda x: -x.started_at)]

    def load_from_disk(self, experiment_name: str) -> list[ExperimentRun]:
        exp_dir = EXPERIMENTS_DIR / experiment_name
        if not exp_dir.exists():
            return []
        runs = []
        for f in exp_dir.glob("*.json"):
            try:
                d = json.loads(f.read_text())
                run = ExperimentRun(
                    run_id=d["run_id"],
                    experiment_name=d["experiment_name"],
                    params=d.get("params", {}),
                    status=d.get("status", "unknown"),
                    final_metrics=d.get("final_metrics", {}),
                    artifacts=d.get("artifacts", {}),
                    tags=d.get("tags", []),
                    started_at=d.get("started_at", 0),
                    ended_at=d.get("ended_at", 0),
                    notes=d.get("notes", ""),
                )
                for mh in d.get("metrics_history", []):
                    run.metrics_history.append(
                        RunMetrics(step=mh["step"], metrics=mh["metrics"], ts=mh["ts"])
                    )
                self._runs[run.run_id] = run
                runs.append(run)
            except Exception as e:
                log.warning(f"Load run error {f}: {e}")
        return runs

    def compare(
        self,
        experiment_name: str,
        metric: str,
        higher_is_better: bool = True,
    ) -> list[dict]:
        runs = [r for r in self._runs.values() if r.experiment_name == experiment_name]
        results = []
        for run in runs:
            val = run.best_metric(metric, higher_is_better)
            if val is not None:
                results.append(
                    {
                        "run_id": run.run_id,
                        "params": run.params,
                        "metric": val,
                        "status": run.status,
                        "duration_s": run.duration_s,
                    }
                )
        return sorted(
            results, key=lambda x: -x["metric"] if higher_is_better else x["metric"]
        )


async def main():
    import sys

    tracker = ExperimentTracker()
    await tracker.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        import random

        for lr in [0.001, 0.01, 0.1]:
            run = tracker.start_run(
                "llm_finetuning",
                params={"lr": lr, "batch_size": 32, "epochs": 5},
                tags=["demo"],
            )
            for step in range(10):
                loss = 2.0 * (0.8**step) + random.gauss(0, 0.05)
                acc = 1 - loss / 3
                await tracker.log_metrics(
                    run.run_id, {"loss": loss, "accuracy": acc}, step
                )
            await tracker.end_run(run.run_id)

        results = tracker.compare("llm_finetuning", "accuracy")
        print(f"{'Run':<10} {'lr':>8} {'accuracy':>10} {'duration':>10}")
        print("-" * 42)
        for r in results:
            print(
                f"  {r['run_id']:<10} {r['params']['lr']:>8.3f} {r['metric']:>10.4f} {r['duration_s']:>8.1f}s"
            )

    elif cmd == "list":
        exp = sys.argv[2] if len(sys.argv) > 2 else None
        runs = tracker.list_runs(exp)
        for r in runs:
            print(
                f"  [{r['run_id']}] {r['experiment_name']} {r['status']} {r['duration_s']:.1f}s {r['final_metrics']}"
            )


if __name__ == "__main__":
    asyncio.run(main())
