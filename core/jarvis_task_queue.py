#!/usr/bin/env python3
"""JARVIS Task Queue — SQLite persistent queue with retry logic"""
import sqlite3, json, time, uuid
from datetime import datetime

DB = "/home/turbo/jarvis/core/task_queue.db"

def _conn():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        type TEXT NOT NULL,
        payload TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        priority INTEGER DEFAULT 5,
        attempts INTEGER DEFAULT 0,
        max_attempts INTEGER DEFAULT 3,
        created_at TEXT,
        updated_at TEXT,
        result TEXT
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_status ON tasks(status, priority DESC)")
    c.commit()
    return c

def enqueue(task_type: str, payload: dict, priority: int = 5, max_attempts: int = 3) -> str:
    tid = str(uuid.uuid4())[:8]
    now = datetime.now().isoformat()[:19]
    db = _conn()
    db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)",
               (tid, task_type, json.dumps(payload), "pending", priority, 0, max_attempts, now, now, None))
    db.commit()
    db.close()
    return tid

def dequeue(task_type: str = None) -> dict | None:
    db = _conn()
    q = "SELECT * FROM tasks WHERE status='pending'"
    if task_type:
        q += f" AND type='{task_type}'"
    q += " ORDER BY priority DESC, created_at ASC LIMIT 1"
    row = db.execute(q).fetchone()
    if not row:
        db.close()
        return None
    tid = row[0]
    db.execute("UPDATE tasks SET status='running', attempts=attempts+1, updated_at=? WHERE id=?",
               (datetime.now().isoformat()[:19], tid))
    db.commit()
    db.close()
    return {"id": row[0], "type": row[1], "payload": json.loads(row[2]),
            "attempts": row[5]+1, "max_attempts": row[6]}

def complete(tid: str, result: str = "ok"):
    db = _conn()
    db.execute("UPDATE tasks SET status='done', result=?, updated_at=? WHERE id=?",
               (result[:500], datetime.now().isoformat()[:19], tid))
    db.commit(); db.close()

def fail(tid: str, error: str = ""):
    db = _conn()
    row = db.execute("SELECT attempts, max_attempts FROM tasks WHERE id=?", (tid,)).fetchone()
    if row and row[0] >= row[1]:
        status = "failed"
    else:
        status = "pending"  # retry
    db.execute("UPDATE tasks SET status=?, result=?, updated_at=? WHERE id=?",
               (status, f"ERR:{error[:200]}", datetime.now().isoformat()[:19], tid))
    db.commit(); db.close()

def stats() -> dict:
    db = _conn()
    rows = db.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status").fetchall()
    db.close()
    return {r[0]: r[1] for r in rows}

if __name__ == "__main__":
    tid = enqueue("llm_query", {"prompt": "test", "model": "gemma3"}, priority=8)
    print(f"Enqueued: {tid}")
    task = dequeue()
    print(f"Dequeued: {task}")
    complete(task["id"], "2")
    print("Stats:", stats())
