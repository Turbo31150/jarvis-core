#!/usr/bin/env python3
"""JARVIS Scheduler Cron — Cron-style job scheduler with Redis-backed state"""

import redis
import json
import time
import hashlib

r = redis.Redis(decode_responses=True)

JOB_PREFIX = "jarvis:cron:job:"
INDEX_KEY = "jarvis:cron:index"
NEXT_RUN_KEY = "jarvis:cron:next_runs"
LOG_KEY = "jarvis:cron:log"
STATS_KEY = "jarvis:cron:stats"


def _parse_cron(expr: str) -> dict:
    """Parse simple cron: 'min hour dom mon dow' — supports * and */n."""
    parts = expr.strip().split()
    if len(parts) != 5:
        raise ValueError(f"Invalid cron: {expr}")
    fields = ["minute", "hour", "dom", "month", "dow"]
    return dict(zip(fields, parts))


def _matches_field(value: int, field: str) -> bool:
    if field == "*":
        return True
    if field.startswith("*/"):
        step = int(field[2:])
        return value % step == 0
    if "," in field:
        return value in map(int, field.split(","))
    if "-" in field:
        lo, hi = field.split("-")
        return int(lo) <= value <= int(hi)
    return value == int(field)


def should_run(cron_expr: str, t: float = None) -> bool:
    t = t or time.time()
    tm = time.localtime(t)
    fields = _parse_cron(cron_expr)
    return (
        _matches_field(tm.tm_min, fields["minute"])
        and _matches_field(tm.tm_hour, fields["hour"])
        and _matches_field(tm.tm_mday, fields["dom"])
        and _matches_field(tm.tm_mon, fields["month"])
        and _matches_field(tm.tm_wday, fields["dow"])
    )


def register_job(
    name: str, cron_expr: str, handler: str, args: dict = None, enabled: bool = True
) -> str:
    _parse_cron(cron_expr)  # validate
    jid = hashlib.md5(name.encode()).hexdigest()[:10]
    job = {
        "id": jid,
        "name": name,
        "cron": cron_expr,
        "handler": handler,
        "args": args or {},
        "enabled": enabled,
        "created_at": time.time(),
        "run_count": 0,
        "last_run": None,
        "last_status": None,
    }
    r.setex(f"{JOB_PREFIX}{jid}", 86400 * 365, json.dumps(job))
    r.sadd(INDEX_KEY, jid)
    r.hincrby(STATS_KEY, "jobs_registered", 1)
    return jid


def get_due_jobs(t: float = None) -> list:
    t = t or time.time()
    jids = r.smembers(INDEX_KEY)
    due = []
    for jid in jids:
        raw = r.get(f"{JOB_PREFIX}{jid}")
        if not raw:
            continue
        job = json.loads(raw)
        if not job.get("enabled"):
            continue
        if should_run(job["cron"], t):
            due.append(job)
    return due


def mark_ran(name: str, success: bool, duration_ms: float = 0):
    jid = hashlib.md5(name.encode()).hexdigest()[:10]
    raw = r.get(f"{JOB_PREFIX}{jid}")
    if not raw:
        return
    job = json.loads(raw)
    job["run_count"] = job.get("run_count", 0) + 1
    job["last_run"] = time.time()
    job["last_status"] = "ok" if success else "failed"
    r.setex(f"{JOB_PREFIX}{jid}", 86400 * 365, json.dumps(job))

    r.lpush(
        LOG_KEY,
        json.dumps(
            {
                "job": name,
                "success": success,
                "duration_ms": duration_ms,
                "ts": time.time(),
            }
        ),
    )
    r.ltrim(LOG_KEY, 0, 999)
    r.hincrby(STATS_KEY, "runs_ok" if success else "runs_failed", 1)


def enable_job(name: str, enabled: bool = True):
    jid = hashlib.md5(name.encode()).hexdigest()[:10]
    raw = r.get(f"{JOB_PREFIX}{jid}")
    if raw:
        job = json.loads(raw)
        job["enabled"] = enabled
        r.setex(f"{JOB_PREFIX}{jid}", 86400 * 365, json.dumps(job))


def list_jobs() -> list:
    jids = r.smembers(INDEX_KEY)
    jobs = []
    for jid in jids:
        raw = r.get(f"{JOB_PREFIX}{jid}")
        if raw:
            jobs.append(json.loads(raw))
    return sorted(jobs, key=lambda x: x["name"])


def recent_log(limit: int = 10) -> list:
    return [json.loads(x) for x in r.lrange(LOG_KEY, 0, limit - 1)]


def stats() -> dict:
    s = r.hgetall(STATS_KEY)
    return {k: int(v) for k, v in s.items()}


if __name__ == "__main__":
    register_job("warmup_models", "*/5 * * * *", "jarvis_model_warmup.warmup_all")
    register_job("telemetry_snap", "* * * * *", "jarvis_telemetry.collect")
    register_job("health_check", "*/2 * * * *", "jarvis_autohealer_v2.run_cycle")
    register_job("kb_compaction", "0 3 * * *", "jarvis_knowledge_base.compact")
    register_job("weekly_report", "0 9 * * 1", "jarvis_reports.weekly")
    register_job("token_reset", "0 0 * * *", "jarvis_token_counter.reset_daily")
    register_job("cache_evict", "*/30 * * * *", "jarvis_cache_warmer.evict_cold")

    # Simulate checking at different times
    test_times = [
        (
            "every minute",
            time.mktime(time.strptime("2026-04-14 10:00:00", "%Y-%m-%d %H:%M:%S")),
        ),
        (
            "5-min mark",
            time.mktime(time.strptime("2026-04-14 10:05:00", "%Y-%m-%d %H:%M:%S")),
        ),
        (
            "3am daily",
            time.mktime(time.strptime("2026-04-14 03:00:00", "%Y-%m-%d %H:%M:%S")),
        ),
        (
            "monday 9am",
            time.mktime(time.strptime("2026-04-14 09:00:00", "%Y-%m-%d %H:%M:%S")),
        ),
    ]

    print("Job schedule check:")
    for label, t in test_times:
        due = get_due_jobs(t)
        names = [j["name"] for j in due]
        print(f"  {label:20s} → {names or '(none)'}")

    # Simulate runs
    for job in list_jobs():
        mark_ran(job["name"], success=True, duration_ms=42)

    print("\nAll jobs:")
    for job in list_jobs():
        status = job.get("last_status", "—")
        icon = "✅" if status == "ok" else ("❌" if status == "failed" else "○")
        print(f"  {icon} {job['name']:20s} {job['cron']:15s} runs={job['run_count']}")

    print(f"\nStats: {stats()}")
