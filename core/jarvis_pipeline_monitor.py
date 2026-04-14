#!/usr/bin/env python3
"""
jarvis_pipeline_monitor — Monitor domino chains, detect stalls, measure throughput
Tracks pipeline stage latencies, identifies bottlenecks, alerts on timeouts
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.pipeline_monitor")

REDIS_PREFIX = "jarvis:pipeline:"
STALL_THRESHOLD_S = 60.0  # stage stall alert
TIMEOUT_THRESHOLD_S = 300.0  # pipeline timeout alert
HISTORY_FILE = Path("/home/turbo/IA/Core/jarvis/data/pipeline_history.json")


@dataclass
class StageRun:
    stage_name: str
    started_at: float
    ended_at: float = 0.0
    status: str = "running"  # running | ok | error | timeout
    error: str = ""
    output_size: int = 0

    @property
    def duration_s(self) -> float:
        end = self.ended_at or time.time()
        return round(end - self.started_at, 3)

    @property
    def is_stalled(self) -> bool:
        return self.status == "running" and self.duration_s > STALL_THRESHOLD_S


@dataclass
class PipelineRun:
    run_id: str
    pipeline_name: str
    started_at: float = field(default_factory=time.time)
    ended_at: float = 0.0
    status: str = "running"
    stages: list[StageRun] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @property
    def duration_s(self) -> float:
        end = self.ended_at or time.time()
        return round(end - self.started_at, 3)

    @property
    def current_stage(self) -> StageRun | None:
        running = [s for s in self.stages if s.status == "running"]
        return running[-1] if running else None

    @property
    def is_timed_out(self) -> bool:
        return self.status == "running" and self.duration_s > TIMEOUT_THRESHOLD_S

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "pipeline_name": self.pipeline_name,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "status": self.status,
            "duration_s": self.duration_s,
            "stages": [
                {
                    "name": s.stage_name,
                    "duration_s": s.duration_s,
                    "status": s.status,
                    "error": s.error,
                }
                for s in self.stages
            ],
            "metadata": self.metadata,
        }


class PipelineMonitor:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._runs: dict[str, PipelineRun] = {}
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    async def start_run(
        self, run_id: str, pipeline_name: str, metadata: dict | None = None
    ) -> PipelineRun:
        run = PipelineRun(
            run_id=run_id,
            pipeline_name=pipeline_name,
            metadata=metadata or {},
        )
        self._runs[run_id] = run
        await self._save_run(run)
        log.info(f"Pipeline started: {pipeline_name} [{run_id}]")
        return run

    async def start_stage(self, run_id: str, stage_name: str) -> StageRun | None:
        run = self._runs.get(run_id)
        if not run:
            return None
        stage = StageRun(stage_name=stage_name, started_at=time.time())
        run.stages.append(stage)
        await self._save_run(run)
        log.debug(f"Stage started: {pipeline_name_of(run)}/{stage_name}")
        return stage

    async def end_stage(
        self,
        run_id: str,
        stage_name: str,
        status: str = "ok",
        error: str = "",
        output_size: int = 0,
    ) -> StageRun | None:
        run = self._runs.get(run_id)
        if not run:
            return None
        # Find last matching stage
        stage = next(
            (
                s
                for s in reversed(run.stages)
                if s.stage_name == stage_name and s.status == "running"
            ),
            None,
        )
        if not stage:
            return None
        stage.ended_at = time.time()
        stage.status = status
        stage.error = error
        stage.output_size = output_size
        await self._save_run(run)

        if status == "error":
            log.error(f"Stage error: {stage_name} [{run_id}]: {error}")
        else:
            log.debug(f"Stage done: {stage_name} [{run_id}] {stage.duration_s:.2f}s")
        return stage

    async def end_run(
        self, run_id: str, status: str = "ok", error: str = ""
    ) -> PipelineRun | None:
        run = self._runs.get(run_id)
        if not run:
            return None
        run.ended_at = time.time()
        run.status = status
        await self._save_run(run)

        log.info(
            f"Pipeline {'✅' if status == 'ok' else '❌'} {run.pipeline_name} "
            f"[{run_id}] {run.duration_s:.2f}s"
        )

        # Publish event
        if self.redis:
            await self.redis.publish(
                "jarvis:events",
                json.dumps(
                    {
                        "event": "pipeline_complete",
                        "run_id": run_id,
                        "pipeline": run.pipeline_name,
                        "status": status,
                        "duration_s": run.duration_s,
                    }
                ),
            )

        return run

    async def _save_run(self, run: PipelineRun):
        if self.redis:
            await self.redis.set(
                f"{REDIS_PREFIX}{run.run_id}",
                json.dumps(run.to_dict()),
                ex=3600,
            )
            await self.redis.zadd(
                f"{REDIS_PREFIX}index",
                {run.run_id: run.started_at},
            )

    async def detect_stalls(self) -> list[dict]:
        stalled = []
        for run in self._runs.values():
            if run.status != "running":
                continue
            if run.is_timed_out:
                stalled.append(
                    {
                        "run_id": run.run_id,
                        "pipeline": run.pipeline_name,
                        "type": "timeout",
                        "duration_s": run.duration_s,
                    }
                )
            elif run.current_stage and run.current_stage.is_stalled:
                stalled.append(
                    {
                        "run_id": run.run_id,
                        "pipeline": run.pipeline_name,
                        "type": "stall",
                        "stage": run.current_stage.stage_name,
                        "stage_duration_s": run.current_stage.duration_s,
                    }
                )
        return stalled

    async def stats(self, pipeline_name: str | None = None) -> dict:
        runs = list(self._runs.values())
        if pipeline_name:
            runs = [r for r in runs if r.pipeline_name == pipeline_name]

        completed = [r for r in runs if r.status in ("ok", "error")]
        ok = [r for r in completed if r.status == "ok"]

        if not completed:
            return {"runs": len(runs), "completed": 0}

        durations = [r.duration_s for r in completed]
        return {
            "pipeline": pipeline_name or "all",
            "total_runs": len(runs),
            "completed": len(completed),
            "success_rate": round(len(ok) / len(completed) * 100, 1),
            "avg_duration_s": round(sum(durations) / len(durations), 2),
            "min_duration_s": round(min(durations), 2),
            "max_duration_s": round(max(durations), 2),
            "running": len([r for r in runs if r.status == "running"]),
        }

    async def recent_runs(self, limit: int = 10) -> list[dict]:
        if self.redis:
            ids = await self.redis.zrevrange(f"{REDIS_PREFIX}index", 0, limit - 1)
            result = []
            for rid in ids:
                raw = await self.redis.get(f"{REDIS_PREFIX}{rid}")
                if raw:
                    result.append(json.loads(raw))
            return result
        return [
            r.to_dict()
            for r in sorted(self._runs.values(), key=lambda x: -x.started_at)[:limit]
        ]


def pipeline_name_of(run: PipelineRun) -> str:
    return run.pipeline_name


# ── CLI ───────────────────────────────────────────────────────────────────────


async def main():
    import sys

    mon = PipelineMonitor()
    await mon.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "recent"

    if cmd == "recent":
        runs = await mon.recent_runs(limit=15)
        if not runs:
            print("No pipeline runs")
        else:
            print(
                f"{'Run ID':<14} {'Pipeline':<24} {'Status':>8} {'Duration':>10} {'Stages':>7}"
            )
            print("-" * 68)
            for r in runs:
                status = (
                    "✅"
                    if r["status"] == "ok"
                    else ("🔄" if r["status"] == "running" else "❌")
                )
                print(
                    f"{r['run_id']:<14} {r['pipeline_name']:<24} {status:>8} "
                    f"{r['duration_s']:>9.2f}s {len(r['stages']):>7}"
                )

    elif cmd == "stats":
        pipeline = sys.argv[2] if len(sys.argv) > 2 else None
        s = await mon.stats(pipeline)
        print(json.dumps(s, indent=2))

    elif cmd == "stalls":
        stalls = await mon.detect_stalls()
        if not stalls:
            print("No stalls detected")
        else:
            for s in stalls:
                print(f"  [{s['type'].upper()}] {s['pipeline']} [{s['run_id']}]")

    elif cmd == "demo":
        # Demo: run a fake pipeline
        import uuid

        run_id = str(uuid.uuid4())[:8]
        run = await mon.start_run(run_id, "demo_pipeline")
        for stage in ["fetch", "process", "store"]:
            await mon.start_stage(run_id, stage)
            await asyncio.sleep(0.1)
            await mon.end_stage(run_id, stage)
        await mon.end_run(run_id)
        r = await mon.recent_runs(1)
        print(json.dumps(r[0] if r else {}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
