#!/usr/bin/env python3
"""JARVIS Memory Consolidator — Flush Redis events → SQLite for long-term storage"""
import redis, sqlite3, json, time
from datetime import datetime

r = redis.Redis(decode_responses=True)
DB = "/home/turbo/jarvis/core/jarvis_master_index.db"

def _ensure_table():
    db = sqlite3.connect(DB)
    db.execute("""CREATE TABLE IF NOT EXISTS event_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT, severity TEXT, source TEXT,
        data TEXT, ts TEXT
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_evlog_ts ON event_log(ts)")
    db.commit()
    return db

def consolidate() -> int:
    """Move Redis event_log → SQLite, keep last 100 in Redis"""
    db = _ensure_table()
    raw_events = r.lrange("jarvis:event_log", 0, -1)
    if not raw_events:
        db.close()
        return 0
    count = 0
    for ev in raw_events:
        try:
            d = json.loads(ev)
            db.execute("INSERT INTO event_log (type, severity, source, data, ts) VALUES (?,?,?,?,?)",
                (d.get("type",""), d.get("severity","info"),
                 d.get("source",""), json.dumps(d.get("data",{})), d.get("ts","")))
            count += 1
        except:
            pass
    db.commit()
    db.close()
    # Keep only last 100 in Redis
    r.ltrim("jarvis:event_log", 0, 99)
    return count

if __name__ == "__main__":
    n = consolidate()
    print(f"Consolidated {n} events to SQLite")
