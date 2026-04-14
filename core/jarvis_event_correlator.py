#!/usr/bin/env python3
"""JARVIS Event Correlator — Detect correlated event patterns (A then B = incident)"""
import redis, json
from datetime import datetime, timedelta

r = redis.Redis(decode_responses=True)

# Patterns: sequence of events within time window → incident type
PATTERNS = [
    {
        "name": "gpu_thermal_cascade",
        "events": ["gpu_thermal_warning", "gpu_thermal_warning"],
        "window_s": 120,
        "action": "trigger_playbook:high_gpu_temp",
        "cooldown_s": 300
    },
    {
        "name": "node_llm_down",
        "events": ["node_down", "circuit_open"],
        "window_s": 60,
        "action": "alert:node_and_llm_both_down",
        "cooldown_s": 600
    },
    {
        "name": "ram_disk_pressure",
        "events": ["ram_critical", "disk_full"],
        "window_s": 300,
        "action": "alert:resource_pressure_critical",
        "cooldown_s": 900
    },
]

def check_patterns(new_event_type: str) -> list:
    triggered = []
    now_ts = datetime.now().timestamp()
    
    for pattern in PATTERNS:
        # Check cooldown
        cd_key = f"jarvis:correlator:cd:{pattern['name']}"
        if r.exists(cd_key):
            continue
        
        if new_event_type not in pattern["events"]:
            continue
        
        # Check if all events in pattern occurred within window
        window = pattern["window_s"]
        all_present = True
        for req_event in set(pattern["events"]):
            count_needed = pattern["events"].count(req_event)
            found = 0
            # Look through recent events
            recent = r.lrange("jarvis:event_log", 0, 99)
            for raw in recent:
                try:
                    ev = json.loads(raw)
                    ev_ts = datetime.fromisoformat(ev.get("ts", "2000-01-01")).timestamp()
                    if ev.get("type") == req_event and now_ts - ev_ts <= window:
                        found += 1
                except: pass
            if found < count_needed:
                all_present = False
                break
        
        if all_present:
            r.setex(cd_key, pattern["cooldown_s"], "1")
            triggered.append({
                "pattern": pattern["name"],
                "action": pattern["action"],
                "ts": datetime.now().isoformat()[:19]
            })
            r.lpush("jarvis:correlator:triggered", json.dumps(triggered[-1]))
    
    return triggered

if __name__ == "__main__":
    print("Event correlator patterns:", len(PATTERNS))
    for p in PATTERNS:
        print(f"  {p['name']}: {p['events']} within {p['window_s']}s → {p['action']}")
