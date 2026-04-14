#!/usr/bin/env python3
"""JARVIS Pattern Detector — Detect recurring patterns in system behavior"""

import redis
import json
import time
from collections import defaultdict
from datetime import datetime

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:patterns"

# Pattern definitions
PATTERNS = {
    "daily_peak": {
        "description": "High CPU/GPU usage recurring daily at same hour",
        "window_h": 168,  # 7 days history
        "threshold": 3,   # must occur 3+ times
    },
    "llm_timeout_spike": {
        "description": "Multiple LLM timeouts in short window",
        "window_m": 10,
        "threshold": 3,
    },
    "score_degradation": {
        "description": "Score declining over consecutive checks",
        "window_checks": 5,
        "decline_threshold": 10,
    },
    "node_flapping": {
        "description": "Node going up/down repeatedly",
        "window_m": 60,
        "threshold": 3,
    },
}


def record_event(event_type: str, metadata: dict = None):
    """Record an event for pattern analysis"""
    ts = time.time()
    entry = json.dumps({"ts": ts, "type": event_type, "meta": metadata or {}})
    r.lpush(f"{PREFIX}:events:{event_type}", entry)
    r.ltrim(f"{PREFIX}:events:{event_type}", 0, 199)
    r.expire(f"{PREFIX}:events:{event_type}", 86400 * 7)


def detect_llm_timeout_spike() -> dict:
    raw = r.lrange(f"{PREFIX}:events:llm_timeout", 0, 49)
    now = time.time()
    window = 600  # 10 min
    recent = [e for e in raw if now - json.loads(e)["ts"] < window]
    triggered = len(recent) >= PATTERNS["llm_timeout_spike"]["threshold"]
    return {"pattern": "llm_timeout_spike", "count": len(recent), "triggered": triggered}


def detect_score_degradation() -> dict:
    raw = r.lrange("jarvis:score_history", 0, 9)
    scores = []
    for item in raw:
        try:
            d = json.loads(item)
            scores.append(d.get("total", 0))
        except Exception:
            pass
    if len(scores) < 3:
        return {"pattern": "score_degradation", "triggered": False, "reason": "insufficient_history"}
    decline = scores[0] - scores[-1]
    triggered = decline >= PATTERNS["score_degradation"]["decline_threshold"]
    return {"pattern": "score_degradation", "triggered": triggered, "decline": decline, "scores": scores[:5]}


def detect_node_flapping(node: str = "M2") -> dict:
    raw = r.lrange(f"{PREFIX}:events:node_status_change", 0, 49)
    now = time.time()
    window = 3600  # 1 hour
    recent = [e for e in raw
              if now - json.loads(e)["ts"] < window
              and json.loads(e).get("meta", {}).get("node") == node]
    triggered = len(recent) >= PATTERNS["node_flapping"]["threshold"]
    return {"pattern": "node_flapping", "node": node, "changes": len(recent), "triggered": triggered}


def analyze_hourly_load() -> dict:
    """Analyze Redis score history to detect daily peak patterns"""
    by_hour = defaultdict(list)
    for key in r.scan_iter("jarvis:score:hourly:*"):
        try:
            parts = key.split(":")
            hour = int(parts[-1])
            val = r.get(key)
            if val:
                by_hour[hour].append(float(val))
        except Exception:
            pass
    peak_hours = []
    for h, vals in by_hour.items():
        avg = sum(vals) / len(vals)
        if avg > 70:  # avg score < 70 = high load
            peak_hours.append({"hour": h, "avg_score": round(avg, 1), "samples": len(vals)})
    return {"pattern": "daily_peak", "peak_hours": sorted(peak_hours, key=lambda x: x["avg_score"])}


def run_all() -> dict:
    detections = [
        detect_llm_timeout_spike(),
        detect_score_degradation(),
        detect_node_flapping("M2"),
        detect_node_flapping("M1"),
        analyze_hourly_load(),
    ]
    triggered = [d for d in detections if d.get("triggered")]
    result = {
        "ts": datetime.now().isoformat()[:19],
        "checked": len(detections),
        "triggered": len(triggered),
        "patterns": detections,
    }
    r.setex(f"{PREFIX}:last", 300, json.dumps(result))
    return result


if __name__ == "__main__":
    res = run_all()
    print(f"Patterns: {res['triggered']}/{res['checked']} triggered")
    for p in res["patterns"]:
        icon = "🔴" if p.get("triggered") else "⚪"
        print(f"  {icon} {p['pattern']}: {p}")
