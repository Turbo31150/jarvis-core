#!/usr/bin/env python3
"""JARVIS Task Graph — DAG task dependency graph with topological execution order"""

import redis
import json
import time
import hashlib
from collections import deque

r = redis.Redis(decode_responses=True)

TASK_PREFIX = "jarvis:tgraph:task:"
GRAPH_KEY = "jarvis:tgraph:graph"
STATS_KEY = "jarvis:tgraph:stats"


def _tid(name: str) -> str:
    return hashlib.md5(name.encode()).hexdigest()[:12]


def add_task(
    name: str, fn_ref: str, args: dict = None, deps: list = None, priority: int = 5
) -> str:
    tid = _tid(name)
    task = {
        "id": tid,
        "name": name,
        "fn_ref": fn_ref,
        "args": args or {},
        "deps": deps or [],
        "priority": priority,
        "status": "pending",
        "created_at": time.time(),
        "result": None,
    }
    r.setex(f"{TASK_PREFIX}{tid}", 3600, json.dumps(task))
    # Store adjacency
    for dep in deps or []:
        dep_tid = _tid(dep)
        r.sadd(f"jarvis:tgraph:edges:{dep_tid}", tid)
    r.sadd(GRAPH_KEY, tid)
    r.hincrby(STATS_KEY, "tasks_added", 1)
    return tid


def _get_task(tid: str) -> dict | None:
    raw = r.get(f"{TASK_PREFIX}{tid}")
    return json.loads(raw) if raw else None


def _set_task(task: dict):
    r.setex(f"{TASK_PREFIX}{task['id']}", 3600, json.dumps(task))


def topological_order() -> list:
    """Return tasks in valid execution order (Kahn's algorithm)."""
    all_tids = list(r.smembers(GRAPH_KEY))
    tasks = {tid: _get_task(tid) for tid in all_tids}
    tasks = {k: v for k, v in tasks.items() if v}

    # Build in-degree map
    in_degree = {tid: 0 for tid in tasks}
    for tid, task in tasks.items():
        for dep in task.get("deps", []):
            dep_tid = _tid(dep)
            if dep_tid in in_degree:
                in_degree[tid] = in_degree.get(tid, 0) + 1

    queue = deque(tid for tid, deg in in_degree.items() if deg == 0)
    order = []
    while queue:
        # Pick highest priority among ready tasks
        ready = sorted(queue, key=lambda t: -tasks[t].get("priority", 5))
        current = ready[0]
        queue.remove(current)
        order.append(current)
        # Reduce in-degree for dependents
        for dependent in r.smembers(f"jarvis:tgraph:edges:{current}"):
            if dependent in in_degree:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

    return [tasks[tid] for tid in order]


def mark_done(name: str, result=None):
    tid = _tid(name)
    task = _get_task(tid)
    if task:
        task["status"] = "done"
        task["result"] = result
        task["finished_at"] = time.time()
        _set_task(task)
        r.hincrby(STATS_KEY, "tasks_done", 1)


def mark_failed(name: str, error: str):
    tid = _tid(name)
    task = _get_task(tid)
    if task:
        task["status"] = "failed"
        task["error"] = error
        _set_task(task)
        r.hincrby(STATS_KEY, "tasks_failed", 1)


def get_ready_tasks() -> list:
    """Tasks whose deps are all done."""
    order = topological_order()
    done = set()
    ready = []
    for task in order:
        if task["status"] == "done":
            done.add(task["name"])
        elif task["status"] == "pending":
            if all(dep in done for dep in task.get("deps", [])):
                ready.append(task)
    return ready


def clear_graph():
    tids = r.smembers(GRAPH_KEY)
    for tid in tids:
        r.delete(f"{TASK_PREFIX}{tid}")
        r.delete(f"jarvis:tgraph:edges:{tid}")
    r.delete(GRAPH_KEY)


def stats() -> dict:
    s = r.hgetall(STATS_KEY)
    total = r.scard(GRAPH_KEY)
    return {"total_tasks": total, **{k: int(v) for k, v in s.items()}}


if __name__ == "__main__":
    clear_graph()

    # Build a pipeline DAG
    add_task("fetch_data", "pipeline.fetch", priority=8)
    add_task("validate_data", "pipeline.validate", deps=["fetch_data"], priority=7)
    add_task("embed_data", "pipeline.embed", deps=["validate_data"], priority=6)
    add_task("store_kb", "pipeline.store", deps=["embed_data"], priority=5)
    add_task("warmup_models", "warmup.all", priority=9)
    add_task("run_rag", "rag.query", deps=["store_kb", "warmup_models"], priority=7)
    add_task("notify", "notif.send", deps=["run_rag"], priority=3)

    order = topological_order()
    print(f"Execution order ({len(order)} tasks):")
    for i, t in enumerate(order):
        deps = ", ".join(t["deps"]) or "—"
        print(f"  {i + 1}. [{t['priority']}] {t['name']:20s} deps={deps}")

    ready = get_ready_tasks()
    print(f"\nReady to run: {[t['name'] for t in ready]}")

    mark_done("fetch_data", {"rows": 1000})
    mark_done("warmup_models", {"ok": 3})
    ready2 = get_ready_tasks()
    print(f"After fetch+warmup done: {[t['name'] for t in ready2]}")
    print(f"\nStats: {stats()}")
