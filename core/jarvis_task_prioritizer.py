#!/usr/bin/env python3
"""JARVIS Task Prioritizer — Score and rank pending tasks using multi-factor priority"""

import redis
import json
import time

r = redis.Redis(decode_responses=True)

QUEUE_KEY = "jarvis:task_prioritizer:queue"
DONE_KEY = "jarvis:task_prioritizer:done"

# Weight factors (sum = 1.0)
WEIGHTS = {
    "urgency": 0.35,
    "impact": 0.30,
    "effort_inv": 0.20,  # inverse of effort (low effort = high score)
    "dependency": 0.15,  # penalty if blocked
}


def add_task(
    task_id: str,
    title: str,
    urgency: int,
    impact: int,
    effort: int,
    blocked_by: list = None,
    tags: list = None,
) -> float:
    """Add a task with scoring. urgency/impact/effort all 1-10."""
    effort_inv = 11 - effort  # invert: effort=1 → score=10, effort=10 → score=1
    dep_score = 0 if blocked_by else 10
    raw_score = (
        urgency * WEIGHTS["urgency"]
        + impact * WEIGHTS["impact"]
        + effort_inv * WEIGHTS["effort_inv"]
        + dep_score * WEIGHTS["dependency"]
    )
    priority = round(raw_score * 10)  # 0-100
    payload = {
        "id": task_id,
        "title": title,
        "urgency": urgency,
        "impact": impact,
        "effort": effort,
        "blocked_by": blocked_by or [],
        "tags": tags or [],
        "priority": priority,
        "added_at": time.time(),
    }
    r.zadd(QUEUE_KEY, {json.dumps(payload): priority})
    return priority


def get_top(n: int = 10, tag: str = None) -> list:
    """Get top-N highest priority tasks."""
    raw = r.zrevrange(QUEUE_KEY, 0, 99, withscores=True)
    results = []
    for item, score in raw:
        task = json.loads(item)
        if tag and tag not in task.get("tags", []):
            continue
        if task.get("blocked_by"):
            # Check if blockers are done
            done = r.smembers(DONE_KEY)
            still_blocked = [b for b in task["blocked_by"] if b not in done]
            if still_blocked:
                task["blocked_by"] = still_blocked
                task["priority"] = max(0, task["priority"] - 20)
                continue
        results.append(task)
        if len(results) >= n:
            break
    return results


def complete_task(task_id: str):
    """Mark task as done, remove from queue."""
    r.sadd(DONE_KEY, task_id)
    raw = r.zrange(QUEUE_KEY, 0, -1)
    for item in raw:
        task = json.loads(item)
        if task["id"] == task_id:
            r.zrem(QUEUE_KEY, item)
            break


def stats() -> dict:
    total = r.zcard(QUEUE_KEY)
    done = r.scard(DONE_KEY)
    top = get_top(3)
    return {
        "pending": total,
        "done": done,
        "top_3": [
            {"id": t["id"], "title": t["title"][:40], "priority": t["priority"]}
            for t in top
        ],
    }


if __name__ == "__main__":
    tasks = [
        ("t1", "Fix M2 timeout fallback", 9, 8, 3, [], ["infra"]),
        ("t2", "Add Prometheus alerts", 6, 7, 5, [], ["monitoring"]),
        ("t3", "Write trading backtester", 7, 9, 8, ["t1"], ["trading"]),
        ("t4", "Update CLAUDE.md", 4, 5, 1, [], ["docs"]),
        ("t5", "Optimize vector search", 5, 6, 4, [], ["ai"]),
        ("t6", "Deploy new API gateway", 8, 9, 6, ["t1", "t2"], ["infra"]),
    ]
    for args in tasks:
        p = add_task(*args)
        print(f"  [{p:3d}] {args[1]}")
    print("\nTop tasks:")
    for t in get_top(4):
        print(f"  [{t['priority']:3d}] {t['title']}")
    print(f"\nStats: {stats()}")
