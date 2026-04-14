#!/usr/bin/env python3
"""JARVIS Task Dispatcher — Intelligent task routing to workers with load balancing"""

import redis
import json
import time
import uuid
from datetime import datetime

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:dispatcher"

# Worker pool definition
WORKERS = {
    "llm_worker":    {"queue": "jarvis:queue:llm",    "concurrency": 3, "task_types": ["llm", "code", "classify", "summary"]},
    "gpu_worker":    {"queue": "jarvis:queue:gpu",    "concurrency": 2, "task_types": ["inference", "training", "embedding"]},
    "io_worker":     {"queue": "jarvis:queue:io",     "concurrency": 10, "task_types": ["fetch", "scrape", "api", "webhook"]},
    "compute_worker":{"queue": "jarvis:queue:compute","concurrency": 4,  "task_types": ["analytics", "benchmark", "score"]},
    "default_worker":{"queue": "jarvis:queue:default","concurrency": 5,  "task_types": []},
}

TASK_TYPE_MAP = {}
for worker_name, cfg in WORKERS.items():
    for t in cfg["task_types"]:
        TASK_TYPE_MAP[t] = worker_name


def route_task(task_type: str) -> str:
    """Determine best worker for task type"""
    # Check if specific worker is available
    worker_name = TASK_TYPE_MAP.get(task_type, "default_worker")
    worker = WORKERS[worker_name]
    # Check worker load
    queue_depth = r.llen(worker["queue"])
    if queue_depth > worker["concurrency"] * 3:
        # Overloaded — try default
        worker_name = "default_worker"
    return worker_name


def dispatch(task_type: str, payload: dict, priority: int = 5, timeout_s: int = 60) -> str:
    tid = f"task:{uuid.uuid4().hex[:12]}"
    worker_name = route_task(task_type)
    worker = WORKERS[worker_name]

    task = {
        "id": tid,
        "type": task_type,
        "payload": payload,
        "priority": priority,
        "timeout_s": timeout_s,
        "worker": worker_name,
        "created_at": time.time(),
        "ts": datetime.now().isoformat()[:19],
    }

    # Push to worker queue (lpush = LIFO for priority, but we use score-based for real priority)
    r.lpush(worker["queue"], json.dumps(task))
    r.expire(worker["queue"], 3600)

    # Track in dispatcher registry
    r.hset(f"{PREFIX}:tasks:{tid}", mapping={
        "status": "queued",
        "worker": worker_name,
        "type": task_type,
        "ts": task["ts"],
    })
    r.expire(f"{PREFIX}:tasks:{tid}", 3600)

    # Stats
    r.hincrby(f"{PREFIX}:stats:{worker_name}", "dispatched", 1)
    r.hincrby(f"{PREFIX}:stats:type:{task_type}", "count", 1)
    return tid


def get_task_status(tid: str) -> dict:
    data = r.hgetall(f"{PREFIX}:tasks:{tid}")
    return data if data else {"error": "not found"}


def complete_task(tid: str, result: str = "ok"):
    r.hset(f"{PREFIX}:tasks:{tid}", mapping={"status": "done", "result": result[:100], "completed_at": datetime.now().isoformat()[:19]})
    r.expire(f"{PREFIX}:tasks:{tid}", 300)


def queue_depths() -> dict:
    depths = {}
    for name, cfg in WORKERS.items():
        depths[name] = {"depth": r.llen(cfg["queue"]), "concurrency": cfg["concurrency"]}
    return depths


def stats() -> dict:
    worker_stats = {}
    for name in WORKERS:
        data = r.hgetall(f"{PREFIX}:stats:{name}")
        worker_stats[name] = int(data.get("dispatched", 0))
    return {
        "queues": queue_depths(),
        "dispatched_by_worker": worker_stats,
        "total_dispatched": sum(worker_stats.values()),
    }


if __name__ == "__main__":
    # Test dispatching
    ids = []
    for task_type, payload in [
        ("llm", {"prompt": "hello"}),
        ("inference", {"model": "qwen3", "input": "test"}),
        ("fetch", {"url": "http://example.com"}),
        ("analytics", {"metric": "score"}),
        ("unknown_type", {"data": "test"}),
    ]:
        tid = dispatch(task_type, payload)
        ids.append(tid)
        print(f"  Dispatched {task_type} → {tid}")

    print(f"\nQueue depths: {queue_depths()}")
    s = stats()
    print(f"Total dispatched: {s['total_dispatched']}")
    for name, count in s["dispatched_by_worker"].items():
        if count > 0:
            print(f"  {name}: {count} tasks")
