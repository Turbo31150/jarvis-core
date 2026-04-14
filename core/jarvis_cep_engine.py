#!/usr/bin/env python3
"""JARVIS CEP Engine — Complex Event Processing on Redis Streams"""
import redis, json, time
from datetime import datetime, timedelta
from collections import defaultdict

r = redis.Redis(decode_responses=True)

# CEP Rules: detect complex patterns in event streams
CEP_RULES = [
    {
        "name": "thermal_runaway",
        "description": "3+ GPU temp warnings within 5 minutes",
        "events": ["gpu_thermal_warning", "gpu_thermal_high", "gpu_temp_anomaly"],
        "count": 3,
        "window_s": 300,
        "action": "trigger_playbook:high_gpu_temp",
        "severity": "critical"
    },
    {
        "name": "llm_degradation",
        "description": "Circuit open + canary failure",
        "events": ["circuit_open", "canary_failure"],
        "count": 2,
        "window_s": 120,
        "action": "alert:llm_system_degraded",
        "severity": "high"
    },
    {
        "name": "cascade_failure",
        "description": "Node down + RAM critical within 3 minutes",
        "events": ["node_down", "ram_critical"],
        "count": 2,
        "window_s": 180,
        "action": "alert:cascade_failure_detected",
        "severity": "critical"
    },
    {
        "name": "rapid_autoscale",
        "description": "3 autoscale events in 10 minutes = unstable load",
        "events": ["autoscale"],
        "count": 3,
        "window_s": 600,
        "action": "alert:workload_instability",
        "severity": "warning"
    },
]

def evaluate_rules(new_event: dict) -> list:
    """Evaluate all CEP rules against recent event history"""
    now = time.time()
    triggered = []
    
    for rule in CEP_RULES:
        # Skip if on cooldown
        cd_key = f"jarvis:cep:cd:{rule['name']}"
        if r.exists(cd_key):
            continue
        
        # Count matching events within window
        recent = r.lrange("jarvis:event_log", 0, 199)
        matching_count = 0
        for raw in recent:
            try:
                ev = json.loads(raw)
                ev_ts = datetime.fromisoformat(ev.get("ts", "2000-01-01")).timestamp()
                if ev.get("type") in rule["events"] and now - ev_ts <= rule["window_s"]:
                    matching_count += 1
            except:
                pass
        
        if matching_count >= rule["count"]:
            r.setex(cd_key, rule["window_s"], "1")
            alert = {
                "cep_rule": rule["name"],
                "description": rule["description"],
                "action": rule["action"],
                "severity": rule["severity"],
                "matching_events": matching_count,
                "ts": datetime.now().isoformat()[:19]
            }
            triggered.append(alert)
            r.lpush("jarvis:cep:triggered", json.dumps(alert))
            r.ltrim("jarvis:cep:triggered", 0, 99)
            r.publish("jarvis:events", json.dumps({
                "type": "cep_alert", "data": alert,
                "severity": rule["severity"], "ts": alert["ts"]
            }))
    
    return triggered

def get_recent_triggers(n: int = 10) -> list:
    return [json.loads(t) for t in r.lrange("jarvis:cep:triggered", 0, n-1)]

if __name__ == "__main__":
    print(f"CEP Engine: {len(CEP_RULES)} rules loaded")
    for rule in CEP_RULES:
        print(f"  {rule['name']}: {rule['count']}x [{','.join(rule['events'][:2])}] in {rule['window_s']}s → {rule['action']}")
    
    # Test with dummy event
    result = evaluate_rules({"type": "test"})
    print(f"Triggered: {len(result)} rules")
