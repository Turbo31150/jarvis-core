#!/usr/bin/env python3
"""JARVIS Task Priority Queue — Redis sorted set priority queue with preemption"""
import redis, json, time, uuid
from datetime import datetime

r = redis.Redis(decode_responses=True)
QUEUE_KEY = "jarvis:pq:tasks"
RUNNING_KEY = "jarvis:pq:running"

# Priority levels (lower number = higher priority)
PRIORITY = {"critical": 1, "high": 2, "normal": 5, "low": 8, "background": 10}

def enqueue(task_type: str, payload: dict, priority: str = "normal",
            ttl: int = 3600) -> str:
    tid = uuid.uuid4().hex[:8]
    score = PRIORITY.get(priority, 5) * 1000 + time.time() % 1000  # priority + FIFO within priority
    task = json.dumps({"id": tid, "type": task_type, "payload": payload,
                       "priority": priority, "ts": datetime.now().isoformat()[:19]})
    r.zadd(QUEUE_KEY, {task: score})
    return tid

def dequeue(worker_id: str = "default") -> dict | None:
    items = r.zrange(QUEUE_KEY, 0, 0, withscores=True)
    if not items:
        return None
    task_raw, score = items[0]
    task = json.loads(task_raw)
    r.zrem(QUEUE_KEY, task_raw)
    r.hset(RUNNING_KEY, task["id"],
           json.dumps({**task, "worker": worker_id, "started": datetime.now().isoformat()[:19]}))
    return task

def complete(task_id: str):
    r.hdel(RUNNING_KEY, task_id)

def stats() -> dict:
    queue_size = r.zcard(QUEUE_KEY)
    running = r.hlen(RUNNING_KEY)
    by_priority = {}
    for item in r.zrange(QUEUE_KEY, 0, -1, withscores=True):
        t = json.loads(item[0])
        p = t.get("priority", "normal")
        by_priority[p] = by_priority.get(p, 0) + 1
    return {"queue_size": queue_size, "running": running, "by_priority": by_priority}

if __name__ == "__main__":
    # Test
    t1 = enqueue("llm_query",   {"prompt": "explain AI"},  "normal")
    t2 = enqueue("gpu_check",   {"gpu": 0},                "critical")
    t3 = enqueue("backup",      {"db": "main"},             "background")
    t4 = enqueue("trading_signal", {"symbol": "BTC"},      "high")
    
    print(f"Queued 4 tasks. Stats: {stats()}")
    
    # Dequeue in priority order
    for _ in range(4):
        task = dequeue()
        if task:
            print(f"  [{task['priority']:10}] {task['type']}: {task['id']}")
            complete(task["id"])
