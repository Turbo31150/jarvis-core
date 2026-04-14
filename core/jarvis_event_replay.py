#!/usr/bin/env python3
"""JARVIS Event Replay — Replay Redis event log for debugging and auditing"""

import redis
import json
import time
from datetime import datetime

r = redis.Redis(decode_responses=True)

EVENT_LOG_KEY = "jarvis:event_log"
REPLAY_CHANNEL = "jarvis:replay"


def get_events(n: int = 100, since_ts: str = None, event_type: str = None) -> list:
    raw = r.lrange(EVENT_LOG_KEY, 0, n - 1)
    events = []
    for item in raw:
        try:
            e = json.loads(item)
            if since_ts and e.get("ts", "") < since_ts:
                continue
            if event_type and e.get("type") != event_type:
                continue
            events.append(e)
        except Exception:
            pass
    return events


def replay(events: list, speed: float = 1.0, dry_run: bool = True) -> dict:
    """Replay a list of events — optionally re-publish to Redis channel"""
    if not events:
        return {"replayed": 0}

    replayed = 0
    skipped = 0
    for e in events:
        try:
            if not dry_run:
                r.publish(REPLAY_CHANNEL, json.dumps({**e, "replayed": True}))
            replayed += 1
            if speed < 10:
                time.sleep(0.01 / max(speed, 0.1))
        except Exception:
            skipped += 1

    return {
        "replayed": replayed,
        "skipped": skipped,
        "dry_run": dry_run,
        "channel": REPLAY_CHANNEL if not dry_run else None,
    }


def export_events(n: int = 500, filepath: str = "/tmp/jarvis_events_export.json") -> str:
    events = get_events(n)
    with open(filepath, "w") as f:
        json.dump({"exported_at": datetime.now().isoformat(), "count": len(events), "events": events}, f, indent=2)
    return filepath


def stats() -> dict:
    total = r.llen(EVENT_LOG_KEY)
    events = get_events(200)
    by_type = {}
    by_severity = {}
    for e in events:
        t = e.get("type", "unknown")
        s = e.get("severity", "info")
        by_type[t] = by_type.get(t, 0) + 1
        by_severity[s] = by_severity.get(s, 0) + 1
    return {
        "total_in_log": total,
        "sampled": len(events),
        "by_type": dict(sorted(by_type.items(), key=lambda x: -x[1])[:10]),
        "by_severity": by_severity,
    }


if __name__ == "__main__":
    import sys
    if "--export" in sys.argv:
        path = export_events()
        print(f"Exported → {path}")
    elif "--replay" in sys.argv:
        events = get_events(50)
        res = replay(events, dry_run=False)
        print(f"Replayed: {res}")
    else:
        s = stats()
        print(f"Event log: {s['total_in_log']} total, sampled {s['sampled']}")
        print(f"By severity: {s['by_severity']}")
        print(f"Top types: {list(s['by_type'].items())[:5]}")
