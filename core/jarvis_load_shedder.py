#!/usr/bin/env python3
"""JARVIS Load Shedder — Adaptive load shedding to protect cluster under pressure"""

import redis
import json
import time

r = redis.Redis(decode_responses=True)

STATE_KEY = "jarvis:loadshed:state"
QUEUE_KEY = "jarvis:loadshed:queue"
STATS_KEY = "jarvis:loadshed:stats"

# Thresholds for shedding levels
LEVELS = {
    "normal": {"shed_pct": 0, "max_queue": 200, "max_lat_ms": 5000},
    "mild": {"shed_pct": 20, "max_queue": 100, "max_lat_ms": 3000},
    "moderate": {"shed_pct": 50, "max_queue": 50, "max_lat_ms": 2000},
    "aggressive": {"shed_pct": 80, "max_queue": 20, "max_lat_ms": 1000},
    "critical": {"shed_pct": 95, "max_queue": 5, "max_lat_ms": 500},
}

PRIORITY_EXEMPT = {"critical", "health_check", "auth"}


def get_level() -> str:
    raw = r.get(STATE_KEY)
    if raw:
        state = json.loads(raw)
        return state.get("level", "normal")
    return "normal"


def set_level(level: str, reason: str = ""):
    assert level in LEVELS
    r.setex(
        STATE_KEY,
        300,
        json.dumps({"level": level, "reason": reason, "ts": time.time()}),
    )
    r.hincrby(STATS_KEY, f"level_{level}", 1)


def auto_detect_level(cpu_pct: float, queue_depth: int, p90_lat_ms: float) -> str:
    """Automatically determine shedding level from metrics."""
    if cpu_pct > 90 or queue_depth > 150 or p90_lat_ms > 8000:
        return "critical"
    if cpu_pct > 75 or queue_depth > 80 or p90_lat_ms > 5000:
        return "aggressive"
    if cpu_pct > 60 or queue_depth > 40 or p90_lat_ms > 3000:
        return "moderate"
    if cpu_pct > 45 or queue_depth > 20 or p90_lat_ms > 2000:
        return "mild"
    return "normal"


def should_shed(request_priority: str = "normal", request_type: str = "") -> bool:
    """Return True if this request should be dropped."""
    if request_priority in PRIORITY_EXEMPT or request_type in PRIORITY_EXEMPT:
        return False
    level = get_level()
    shed_pct = LEVELS[level]["shed_pct"]
    if shed_pct == 0:
        return False
    # Deterministic shedding based on current second
    bucket = int(time.time() * 10) % 100
    return bucket < shed_pct


def admit(task_id: str, priority: str = "normal", payload: dict = None) -> bool:
    """Admit a task to the queue. Returns False if shed."""
    if should_shed(priority):
        r.hincrby(STATS_KEY, "shed_count", 1)
        return False
    level = get_level()
    max_q = LEVELS[level]["max_queue"]
    if r.llen(QUEUE_KEY) >= max_q:
        r.hincrby(STATS_KEY, "queue_full_drops", 1)
        return False
    r.rpush(
        QUEUE_KEY,
        json.dumps(
            {
                "id": task_id,
                "priority": priority,
                "payload": payload or {},
                "ts": time.time(),
            }
        ),
    )
    r.hincrby(STATS_KEY, "admitted", 1)
    return True


def drain(batch: int = 10) -> list:
    tasks = []
    for _ in range(batch):
        raw = r.lpop(QUEUE_KEY)
        if not raw:
            break
        tasks.append(json.loads(raw))
    return tasks


def queue_depth() -> int:
    return r.llen(QUEUE_KEY)


def stats() -> dict:
    s = r.hgetall(STATS_KEY)
    return {
        "level": get_level(),
        "queue_depth": queue_depth(),
        **{k: int(v) for k, v in s.items()},
    }


if __name__ == "__main__":

    # Simulate load ramp
    scenarios = [
        ("normal", 20, 5, 800),
        ("mild", 48, 22, 2100),
        ("moderate", 62, 45, 3200),
        ("aggressive", 78, 90, 5500),
        ("critical", 92, 160, 9000),
    ]

    print("Load Shedding Simulation:")
    for name, cpu, queue, lat in scenarios:
        detected = auto_detect_level(cpu, queue, lat)
        set_level(detected, f"cpu={cpu}% q={queue} lat={lat}ms")
        # Try admitting 20 requests
        admitted = sum(1 for i in range(20) if admit(f"req_{i}", "normal"))
        print(
            f"  {detected:12s} cpu={cpu:3d}% q={queue:3d} lat={lat:5d}ms → "
            f"admitted {admitted:2d}/20"
        )

    set_level("normal")
    print(f"\nStats: {stats()}")
