#!/usr/bin/env python3
"""JARVIS Node Rebalancer — Rebalance LLM tasks across nodes when overloaded"""
import redis, json, requests
from datetime import datetime

r = redis.Redis(decode_responses=True)

NODES = {
    "m1": {"host": "http://192.168.1.85:1234",  "max_concurrent": 4, "weight": 3},
    "m2": {"host": "http://192.168.1.26:1234",  "max_concurrent": 3, "weight": 2},
    "ol1":{"host": "http://127.0.0.1:11434",    "max_concurrent": 2, "weight": 1},
}

def get_node_load() -> dict:
    loads = {}
    for name, info in NODES.items():
        active = int(r.get(f"jarvis:node:{name}:active_requests") or 0)
        status = r.get(f"jarvis:node:{name}:status") or "unknown"
        loads[name] = {
            "active": active,
            "max": info["max_concurrent"],
            "utilization": active / info["max_concurrent"] if info["max_concurrent"] else 1.0,
            "status": status,
            "up": status == "up"
        }
    return loads

def recommend_node(task_type: str = "default") -> str:
    loads = get_node_load()
    available = {n: d for n, d in loads.items() if d["up"] and d["utilization"] < 0.9}
    
    if not available:
        # All overloaded — use least utilized
        available = {n: d for n, d in loads.items() if d["up"]}
    
    if not available:
        return "ol1"  # last resort
    
    # For reasoning tasks, prefer m2 (has r1)
    if task_type in ("reasoning", "trading") and "m2" in available:
        return "m2"
    
    # Otherwise: least utilized
    return min(available, key=lambda n: available[n]["utilization"])

def rebalance_check() -> dict:
    loads = get_node_load()
    actions = []
    for name, load in loads.items():
        if load["utilization"] > 0.85:
            alt = recommend_node()
            if alt != name:
                actions.append({"from": name, "to": alt, "reason": f"utilization {load['utilization']:.0%}"})
    
    result = {"ts": datetime.now().isoformat()[:19], "loads": loads, "actions": actions}
    r.setex("jarvis:rebalancer:last", 120, json.dumps(result))
    return result

if __name__ == "__main__":
    result = rebalance_check()
    print("Node loads:")
    for name, load in result["loads"].items():
        status = "✅" if load["up"] else "❌"
        print(f"  {status} {name}: {load['active']}/{load['max']} ({load['utilization']:.0%})")
    if result["actions"]:
        for a in result["actions"]:
            print(f"  → Rebalance {a['from']}→{a['to']}: {a['reason']}")
    else:
        print("  No rebalancing needed")
    print(f"Recommended for 'code': {recommend_node('code')}")
