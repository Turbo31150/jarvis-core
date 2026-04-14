#!/usr/bin/env python3
"""JARVIS Auto Scaler — Dynamically adjust worker pool size based on queue depth"""
import redis, json, time
from datetime import datetime

r = redis.Redis(decode_responses=True)

MIN_WORKERS = 1
MAX_WORKERS = 8
SCALE_UP_THRESHOLD = 10    # tasks in queue
SCALE_DOWN_THRESHOLD = 2

def get_queue_depth() -> int:
    """Count pending tasks across Redis + SQLite queue"""
    # Redis stream pending
    try:
        info = r.xinfo_groups("jarvis:stream")
        pending = sum(g.get("pending", 0) for g in info)
    except:
        pending = 0
    # SQLite task_queue
    try:
        import sqlite3
        db = sqlite3.connect("/home/turbo/jarvis/core/task_queue.db")
        pending += db.execute("SELECT COUNT(*) FROM tasks WHERE status='pending'").fetchone()[0]
        db.close()
    except:
        pass
    return pending

def recommend_workers() -> dict:
    depth = get_queue_depth()
    current = int(r.get("jarvis:workers:count") or MIN_WORKERS)
    
    if depth > SCALE_UP_THRESHOLD and current < MAX_WORKERS:
        target = min(current + 2, MAX_WORKERS)
        action = "scale_up"
    elif depth < SCALE_DOWN_THRESHOLD and current > MIN_WORKERS:
        target = max(current - 1, MIN_WORKERS)
        action = "scale_down"
    else:
        target = current
        action = "hold"
    
    rec = {"queue_depth": depth, "current": current, "target": target, "action": action,
           "ts": datetime.now().isoformat()[:19]}
    r.setex("jarvis:autoscaler", 120, json.dumps(rec))
    
    if action != "hold":
        r.set("jarvis:workers:count", target)
        r.publish("jarvis:events", json.dumps({"type": "autoscale", "data": rec,
                                                "severity": "info", "ts": rec["ts"]}))
    return rec

if __name__ == "__main__":
    rec = recommend_workers()
    print(f"Queue: {rec['queue_depth']} | Workers: {rec['current']}→{rec['target']} [{rec['action']}]")
