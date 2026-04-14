#!/usr/bin/env python3
"""JARVIS Data Exporter — Export metrics/events/scores to CSV/JSON for analysis"""

import redis
import json
import csv
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

r = redis.Redis(decode_responses=True)
EXPORT_DIR = Path("/home/turbo/IA/Core/jarvis/exports")
EXPORT_DIR.mkdir(exist_ok=True)


def export_scores(days: int = 7) -> str:
    """Export score history to CSV"""
    filename = EXPORT_DIR / f"scores_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    raw = r.lrange("jarvis:score_history", 0, days * 24 - 1)
    rows = []
    for item in raw:
        try:
            d = json.loads(item)
            rows.append({
                "ts": d.get("ts", ""),
                "total": d.get("total", 0),
                "cpu_thermal": d.get("cpu_thermal", 0),
                "ram": d.get("ram", 0),
                "gpu": d.get("gpu", 0),
                "llm": d.get("llm", 0),
                "services": d.get("services", 0),
            })
        except Exception:
            pass
    with open(filename, "w", newline="") as f:
        if rows:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
    return str(filename)


def export_events(n: int = 500) -> str:
    """Export recent events to JSON"""
    filename = EXPORT_DIR / f"events_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    raw = r.lrange("jarvis:event_log", 0, n - 1)
    events = []
    for item in raw:
        try:
            events.append(json.loads(item))
        except Exception:
            pass
    with open(filename, "w") as f:
        json.dump({"exported_at": datetime.now().isoformat(), "count": len(events), "events": events}, f, indent=2)
    return str(filename)


def export_metrics_snapshot() -> str:
    """Export current Redis metrics snapshot"""
    filename = EXPORT_DIR / f"metrics_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    metrics = []
    # GPU metrics
    for i in range(5):
        temp = r.get(f"jarvis:gpu:{i}:temp")
        vram = r.get(f"jarvis:gpu:{i}:vram_pct")
        if temp:
            metrics.append({"metric": f"gpu{i}_temp", "value": temp, "unit": "celsius"})
        if vram:
            metrics.append({"metric": f"gpu{i}_vram_pct", "value": vram, "unit": "percent"})
    # LLM router stats
    for key in r.scan_iter("jarvis:llm_router:*:count"):
        parts = key.split(":")
        if len(parts) >= 4:
            metrics.append({"metric": f"llm_calls_{parts[2]}", "value": r.get(key) or 0, "unit": "count"})
    # Score
    score = json.loads(r.get("jarvis:score") or "{}")
    metrics.append({"metric": "jarvis_score", "value": score.get("total", 0), "unit": "points"})

    with open(filename, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["metric", "value", "unit"])
        writer.writeheader()
        writer.writerows(metrics)
    return str(filename)


def list_exports() -> list:
    return sorted([str(p) for p in EXPORT_DIR.glob("*.csv")] + [str(p) for p in EXPORT_DIR.glob("*.json")])


def cleanup_old(days: int = 7) -> int:
    cutoff = time.time() - days * 86400
    deleted = 0
    for filepath in EXPORT_DIR.glob("*"):
        if filepath.stat().st_mtime < cutoff:
            filepath.unlink()
            deleted += 1
    return deleted


if __name__ == "__main__":
    f1 = export_scores()
    f2 = export_metrics_snapshot()
    f3 = export_events(100)
    print(f"Exports created:")
    print(f"  scores:  {f1}")
    print(f"  metrics: {f2}")
    print(f"  events:  {f3}")
    exports = list_exports()
    print(f"Total exports: {len(exports)}")
