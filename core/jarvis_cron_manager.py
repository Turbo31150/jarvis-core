#!/usr/bin/env python3
"""JARVIS Cron Manager — List, add, disable JARVIS cron jobs dynamically"""
import subprocess, redis, json, re
from datetime import datetime

r = redis.Redis(decode_responses=True)

def list_jarvis_crons() -> list:
    try:
        out = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        crons = []
        for line in out.stdout.split("\n"):
            if "jarvis" in line.lower() and not line.startswith("#") and line.strip():
                parts = line.strip().split(None, 5)
                if len(parts) >= 6:
                    crons.append({
                        "schedule": " ".join(parts[:5]),
                        "command": parts[5],
                        "enabled": True
                    })
        return crons
    except:
        return []

def count_crons() -> dict:
    crons = list_jarvis_crons()
    return {"total": len(crons), "enabled": len([c for c in crons if c["enabled"]]),
            "ts": datetime.now().isoformat()[:19]}

def snapshot():
    """Store current cron state in Redis"""
    crons = list_jarvis_crons()
    r.setex("jarvis:crons:snapshot", 3600, json.dumps(crons))
    return crons

if __name__ == "__main__":
    crons = snapshot()
    stats = count_crons()
    print(f"JARVIS crons: {stats['total']} total, {stats['enabled']} enabled")
    for c in crons[:5]:
        print(f"  [{c['schedule']}] {c['command'][:60]}")
    if len(crons) > 5:
        print(f"  ... and {len(crons)-5} more")
