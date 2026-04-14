#!/usr/bin/env python3
"""JARVIS Request Logger — Log all LLM requests for audit trail and replay"""
import redis, sqlite3, json, hashlib
from datetime import datetime

r = redis.Redis(decode_responses=True)
DB = "/home/turbo/jarvis/core/jarvis_master_index.db"

def _ensure_table():
    db = sqlite3.connect(DB)
    db.execute("""CREATE TABLE IF NOT EXISTS llm_request_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id TEXT,
        task_type TEXT,
        backend TEXT,
        model TEXT,
        prompt_hash TEXT,
        response_len INTEGER,
        latency_ms INTEGER,
        ok INTEGER,
        ts TEXT
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_rlog_ts ON llm_request_log(ts)")
    db.commit()
    return db

def log_request(task_type: str, backend: str, model: str, prompt: str,
                response: str, latency_ms: int, ok: bool):
    rid = hashlib.md5(f"{prompt}{datetime.now().isoformat()}".encode()).hexdigest()[:10]
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:12]
    
    db = _ensure_table()
    db.execute(
        "INSERT INTO llm_request_log (request_id,task_type,backend,model,prompt_hash,response_len,latency_ms,ok,ts) VALUES (?,?,?,?,?,?,?,?,?)",
        (rid, task_type, backend, model, prompt_hash, len(response), latency_ms, int(ok), datetime.now().isoformat()[:19])
    )
    db.commit()
    db.close()
    return rid

def stats(last_n: int = 100) -> dict:
    db = _ensure_table()
    total = db.execute("SELECT COUNT(*) FROM llm_request_log").fetchone()[0]
    ok_count = db.execute("SELECT COUNT(*) FROM llm_request_log WHERE ok=1").fetchone()[0]
    avg_lat = db.execute("SELECT AVG(latency_ms) FROM llm_request_log WHERE ok=1 AND latency_ms > 0").fetchone()[0]
    by_backend = dict(db.execute(
        "SELECT backend, COUNT(*) FROM llm_request_log GROUP BY backend"
    ).fetchall())
    db.close()
    return {"total": total, "ok": ok_count, "success_rate": round(ok_count/max(total,1)*100,1),
            "avg_latency_ms": round(avg_lat or 0), "by_backend": by_backend}

if __name__ == "__main__":
    # Add some test entries
    log_request("fast", "ol1", "gemma3:4b", "1+1=", "2", 693, True)
    log_request("code", "m2", "qwen3.5-35b", "Fix this bug", "", -1, False)
    print("Stats:", stats())
