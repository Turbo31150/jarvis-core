#!/usr/bin/env python3
"""JARVIS Backup Manager — Automated SQLite + Redis backup with rotation"""
import os, shutil, sqlite3, redis, json, time, gzip
from datetime import datetime
from pathlib import Path

r = redis.Redis(decode_responses=True)
BACKUP_DIR = Path("/home/turbo/jarvis/backups")
DBS = [
    "/home/turbo/jarvis/core/jarvis_master_index.db",
    "/home/turbo/jarvis/core/scheduler.db",
    "/home/turbo/jarvis/core/task_queue.db",
    "/home/turbo/IA/Core/jarvis/orchestrator/jarvis_orchestrator.db",
]
MAX_BACKUPS = 7  # keep 7 days

def backup_sqlite() -> list:
    BACKUP_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backed = []
    for db_path in DBS:
        if not os.path.exists(db_path):
            continue
        name = Path(db_path).stem
        dest = BACKUP_DIR / f"{name}_{ts}.db.gz"
        try:
            src = sqlite3.connect(db_path)
            tmp = str(dest).replace(".gz", "")
            bak = sqlite3.connect(tmp)
            src.backup(bak)
            src.close(); bak.close()
            with open(tmp, 'rb') as f_in, gzip.open(str(dest), 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)
            os.unlink(tmp)
            backed.append(str(dest))
        except Exception as e:
            print(f"  ERR {name}: {e}")
    return backed

def backup_redis_keys() -> str:
    BACKUP_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = BACKUP_DIR / f"redis_snapshot_{ts}.json.gz"
    data = {}
    for key in r.scan_iter("jarvis:*"):
        t = r.type(key)
        if t == "string":
            data[key] = r.get(key)
        elif t == "hash":
            data[key] = r.hgetall(key)
    with gzip.open(str(dest), 'wt') as f:
        json.dump(data, f)
    return str(dest)

def rotate():
    """Keep only MAX_BACKUPS per DB"""
    for pattern in ["jarvis_master_index_*.db.gz", "scheduler_*.db.gz", "redis_snapshot_*.json.gz"]:
        files = sorted(BACKUP_DIR.glob(pattern))
        for old in files[:-MAX_BACKUPS]:
            old.unlink()

def run():
    print(f"[Backup] Starting at {datetime.now().strftime('%H:%M:%S')}")
    backed = backup_sqlite()
    redis_bak = backup_redis_keys()
    rotate()
    total = sum(1 for _ in BACKUP_DIR.glob("*"))
    print(f"  SQLite: {len(backed)} files | Redis: 1 file | Total backups: {total}")
    r.setex("jarvis:backup:last", 86400, datetime.now().isoformat()[:19])
    return {"sqlite": len(backed), "redis": 1}

if __name__ == "__main__":
    run()
