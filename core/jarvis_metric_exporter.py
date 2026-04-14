#!/usr/bin/env python3
"""JARVIS Metric Exporter — Export Redis metrics to JSON file for external monitoring"""
import redis, json, time, os
from datetime import datetime

r = redis.Redis(decode_responses=True)
EXPORT_PATH = "/tmp/jarvis_metrics.json"

def export():
    metrics = {
        "ts": datetime.now().isoformat()[:19],
        "score": {},
        "gpus": {},
        "ram": {},
        "llm": {},
        "router": {},
    }
    
    # Score
    score_raw = r.get("jarvis:score")
    if score_raw:
        try: metrics["score"] = json.loads(score_raw)
        except: pass
    
    # GPUs
    for i in range(5):
        temp = r.get(f"jarvis:gpu:{i}:temp")
        vram = r.get(f"jarvis:gpu:{i}:vram_pct")
        if temp:
            metrics["gpus"][f"gpu{i}"] = {"temp_c": temp, "vram_pct": vram}
    
    # RAM
    metrics["ram"]["free_gb"] = r.get("jarvis:ram:free_gb")
    
    # LLM latencies
    for key in r.scan_iter("jarvis:llm:*:latency_ms"):
        name = key.split(":")[2]
        metrics["llm"][name] = r.get(key)
    
    # Router stats
    for key in r.scan_iter("jarvis:llm_router:*:count"):
        t = key.split(":")[2]
        metrics["router"][t] = {"count": r.get(key), "last_ms": r.get(f"jarvis:llm_router:{t}:last_ms")}
    
    with open(EXPORT_PATH, "w") as f:
        json.dump(metrics, f, indent=2)
    return metrics

if __name__ == "__main__":
    m = export()
    print(f"Exported to {EXPORT_PATH}")
    print(f"  GPUs: {len(m['gpus'])} | LLMs: {len(m['llm'])} | Score: {m['score'].get('total','?')}")
