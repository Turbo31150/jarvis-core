#!/usr/bin/env python3
"""
jarvis_cron_scheduler — Cron-expression task scheduler with async execution
Parses cron expressions, fires async callbacks, tracks run history
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.cron_scheduler")

REDIS_PREFIX = "jarvis:cron:"
SCHEDULE_FILE = Path("/home/turbo/IA/Core/jarvis/data/cron_schedule.json")


def _matches_field(value: int, expr: str) -> bool:
    """Check if value matches a cron field expression."""
    if expr == "*":
        return True
    if expr.startswith("*/"):
        step = int(expr[2:])
        return value % step == 0
    if "," in expr:
        return any(_matches_field(value, part) for part in expr.split(","))
    if "-" in expr:
        lo, hi = expr.split("-", 1)
        return int(lo) <= value <= int(hi)
    return value == int(expr)


def cron_matches(expression: str, dt: datetime) -> bool:
    """Returns True if datetime matches 5-field cron expression."""
    parts = expression.strip().split()
    if len(parts) != 5:
        return False
    minute, hour, dom, month, dow = parts
    return (
        _matches_field(dt.minute, minute)
        and _matches_field(dt.hour, hour)
        and _matches_field(dt.day, dom)
        and _matches_field(dt.month, month)
        and _matches_field(dt.weekday(), dow)  # 0=Monday
    )


@dataclass
class CronJob:
    job_id: str
    name: str
    expression: str
    handler_key: str
    enabled: bool = True
    description: str = ""
    tags: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_run: float = 0.0
    last_status: str = ""
    run_count: int = 0
    error_count: int = 0

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "name": self.name,
            "expression": self.expression,
            "handler_key": self.handler_key,
            "enabled": self.enabled,
            "description": self.description,
            "tags": self.tags,
            "run_count": self.run_count,
            "error_count": self.error_count,
            "last_run": self.last_run,
            "last_status": self.last_status,
        }


@dataclass
class CronRun:
    run_id: str
    job_id: str
    job_name: str
    started_at: float
    ended_at: float = 0.0
    status: str = "running"
    error: str = ""

    @property
    def duration_ms(self) -> float:
        end = self.ended_at or time.time()
        return round((end - self.started_at) * 1000, 1)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "job_id": self.job_id,
            "job_name": self.job_name,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "error": self.error,
        }


class CronScheduler:
    def __init__(self, tick_s: float = 10.0):
        self.redis: aioredis.Redis | None = None
        self._jobs: dict[str, CronJob] = {}
        self._handlers: dict[str, Callable] = {}
        self._runs: list[CronRun] = []
        self._tick_s = tick_s
        self._running = False
        self._task: asyncio.Task | None = None
        self._last_tick: datetime | None = None
        self._load()

    def _load(self):
        if SCHEDULE_FILE.exists():
            try:
                data = json.loads(SCHEDULE_FILE.read_text())
                for jd in data.get("jobs", []):
                    job = CronJob(
                        **{
                            k: v
                            for k, v in jd.items()
                            if k in CronJob.__dataclass_fields__
                        }
                    )
                    self._jobs[job.job_id] = job
                log.debug(f"Loaded {len(self._jobs)} cron jobs")
            except Exception as e:
                log.warning(f"Schedule load error: {e}")

    def _save(self):
        SCHEDULE_FILE.parent.mkdir(parents=True, exist_ok=True)
        SCHEDULE_FILE.write_text(
            json.dumps({"jobs": [j.to_dict() for j in self._jobs.values()]}, indent=2)
        )

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def register_handler(self, key: str, fn: Callable):
        self._handlers[key] = fn

    def add_job(
        self,
        name: str,
        expression: str,
        handler_key: str,
        description: str = "",
        tags: list[str] | None = None,
        job_id: str | None = None,
    ) -> CronJob:
        job = CronJob(
            job_id=job_id or str(uuid.uuid4())[:8],
            name=name,
            expression=expression,
            handler_key=handler_key,
            description=description,
            tags=tags or [],
        )
        self._jobs[job.job_id] = job
        self._save()
        log.info(f"Cron job added: {name} [{expression}]")
        return job

    def remove_job(self, job_id: str) -> bool:
        if job_id in self._jobs:
            del self._jobs[job_id]
            self._save()
            return True
        return False

    def enable(self, job_id: str):
        if job := self._jobs.get(job_id):
            job.enabled = True
            self._save()

    def disable(self, job_id: str):
        if job := self._jobs.get(job_id):
            job.enabled = False
            self._save()

    async def _fire_job(self, job: CronJob):
        handler = self._handlers.get(job.handler_key)
        if not handler:
            log.warning(f"No handler '{job.handler_key}' for job '{job.name}'")
            return

        run = CronRun(
            run_id=str(uuid.uuid4())[:8],
            job_id=job.job_id,
            job_name=job.name,
            started_at=time.time(),
        )
        self._runs.append(run)
        job.run_count += 1
        job.last_run = run.started_at

        try:
            if asyncio.iscoroutinefunction(handler):
                await handler(job)
            else:
                handler(job)
            run.status = "ok"
            run.ended_at = time.time()
            job.last_status = "ok"
        except Exception as e:
            run.status = "error"
            run.error = str(e)[:200]
            run.ended_at = time.time()
            job.error_count += 1
            job.last_status = f"error: {e}"
            log.error(f"Cron job '{job.name}' failed: {e}")

        self._save()

        if self.redis:
            asyncio.create_task(
                self.redis.lpush(f"{REDIS_PREFIX}runs", json.dumps(run.to_dict()))
            )
            asyncio.create_task(self.redis.ltrim(f"{REDIS_PREFIX}runs", 0, 999))

    async def _tick_loop(self):
        while self._running:
            now = datetime.now().replace(second=0, microsecond=0)

            # Only fire once per minute
            if self._last_tick != now:
                self._last_tick = now
                for job in list(self._jobs.values()):
                    if job.enabled and cron_matches(job.expression, now):
                        asyncio.create_task(self._fire_job(job))

            await asyncio.sleep(self._tick_s)

    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._tick_loop())
        log.info(
            f"CronScheduler started (tick={self._tick_s}s, jobs={len(self._jobs)})"
        )

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def run_now(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job:
            return False
        await self._fire_job(job)
        return True

    def list_jobs(self) -> list[dict]:
        return [j.to_dict() for j in self._jobs.values()]

    def recent_runs(self, n: int = 20) -> list[dict]:
        return [r.to_dict() for r in self._runs[-n:]]

    def stats(self) -> dict:
        return {
            "jobs": len(self._jobs),
            "enabled": sum(1 for j in self._jobs.values() if j.enabled),
            "total_runs": sum(j.run_count for j in self._jobs.values()),
            "total_errors": sum(j.error_count for j in self._jobs.values()),
            "running": self._running,
        }


async def main():
    import sys

    scheduler = CronScheduler(tick_s=2.0)
    await scheduler.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        fired = []

        async def health_check(job: CronJob):
            fired.append(job.name)
            log.info(f"Health check fired: {job.name}")

        scheduler.register_handler("health_check", health_check)
        scheduler.add_job(
            "cluster_health",
            "* * * * *",
            "health_check",
            description="Check cluster health every minute",
        )
        scheduler.add_job(
            "daily_backup",
            "0 2 * * *",
            "health_check",
            description="Daily backup at 2am",
        )

        await scheduler.start()
        await scheduler.run_now(list(scheduler._jobs.keys())[0])
        await asyncio.sleep(0.2)
        await scheduler.stop()

        print(f"Fired: {fired}")
        print(f"Jobs: {json.dumps(scheduler.list_jobs(), indent=2)}")
        print(f"Stats: {json.dumps(scheduler.stats(), indent=2)}")

    elif cmd == "list":
        for j in scheduler.list_jobs():
            status = "✅" if j["enabled"] else "⏸"
            print(
                f"  {status} {j['name']:<25} [{j['expression']}] runs={j['run_count']}"
            )

    elif cmd == "stats":
        print(json.dumps(scheduler.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
