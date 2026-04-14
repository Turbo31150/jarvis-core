#!/usr/bin/env python3
"""JARVIS Data Pipeline — ETL: Redis metrics → SQLite → JSON/CSV export"""
import redis, sqlite3, json, csv, os
from datetime import datetime
from pathlib import Path

r = redis.Redis(decode_responses=True)
DB = "/home/turbo/jarvis/core/jarvis_master_index.db"
EXPORT_DIR = Path("/home/turbo/jarvis/exports")

def _ensure_tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS metrics_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        metric TEXT, value REAL, tags TEXT, ts TEXT
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_mh_ts ON metrics_history(ts)")
    db.commit()

def extract_from_redis() -> list:
    """Extract current metrics from Redis"""
    metrics = []
    ts = datetime.now().isoformat()[:19]
    
    # GPU metrics
    for i in range(5):
        temp = r.get(f"jarvis:gpu:{i}:temp")
        vram = r.get(f"jarvis:gpu:{i}:vram_pct")
        if temp:
            metrics.append(("gpu_temp", float(temp), f'{{"gpu":{i}}}', ts))
        if vram:
            metrics.append(("gpu_vram_pct", float(vram), f'{{"gpu":{i}}}', ts))
    
    # RAM
    ram = r.get("jarvis:ram:free_gb")
    if ram:
        metrics.append(("ram_free_gb", float(ram), "{}", ts))
    
    # Score
    score_raw = r.get("jarvis:score")
    if score_raw:
        score = json.loads(score_raw)
        metrics.append(("system_score", float(score.get("total", 0)), "{}", ts))
    
    # LLM latencies
    for key in r.scan_iter("jarvis:bench_live:*:latency_ms"):
        name = key.split(":")[2]
        val = r.get(key)
        if val:
            metrics.append(("llm_latency_ms", float(val), f'{{"model":"{name}"}}', ts))
    
    return metrics

def load_to_sqlite(metrics: list) -> int:
    db = sqlite3.connect(DB)
    _ensure_tables(db)
    db.executemany("INSERT INTO metrics_history (metric,value,tags,ts) VALUES (?,?,?,?)", metrics)
    db.commit()
    db.close()
    return len(metrics)

def export_csv(hours: int = 1) -> str:
    EXPORT_DIR.mkdir(exist_ok=True)
    db = sqlite3.connect(DB)
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()[:19]
    rows = db.execute(
        "SELECT metric, value, tags, ts FROM metrics_history WHERE ts > ? ORDER BY ts",
        (cutoff,)
    ).fetchall()
    db.close()
    
    fpath = EXPORT_DIR / f"metrics_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    with open(fpath, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["metric", "value", "tags", "ts"])
        w.writerows(rows)
    return str(fpath)

def run():
    metrics = extract_from_redis()
    n = load_to_sqlite(metrics)
    return {"extracted": n, "ts": datetime.now().isoformat()[:19]}

if __name__ == "__main__":
    res = run()
    print(f"Pipeline: extracted {res['extracted']} metrics")
    fpath = export_csv(hours=1)
    print(f"Exported CSV: {fpath}")
