#!/usr/bin/env python3
"""JARVIS Node Watchdog — Surveille M1/M3 et alerte quand ils remontent"""
import requests, time, subprocess, redis, json, os
from datetime import datetime

NODES = {
    "M1": {"host": "http://192.168.1.85:1234", "ssh": "turbo@192.168.1.85", "alias": "lmstudio-m1"},
    "M2": {"host": "http://192.168.1.26:1234", "ssh": "turbo@192.168.1.26", "alias": "lmstudio-m2"},
    "M3": {"host": "http://192.168.1.133:1234", "ssh": "turbo@192.168.1.133", "alias": "lmstudio-m3"},
}
r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)

def check_node(name, node):
    try:
        resp = requests.get(f"{node['host']}/v1/models", timeout=3)
        if resp.status_code == 200:
            models = [m['id'] for m in resp.json().get('data', [])]
            return True, models
    except:
        pass
    return False, []

def try_ssh_start_lmstudio(node):
    """Tenter de relancer LMStudio via SSH"""
    try:
        # Check if SSH accessible
        r = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
             node["ssh"], "echo OK"],
            capture_output=True, text=True, timeout=8
        )
        if r.returncode == 0:
            # Try start LMStudio server
            subprocess.Popen(
                ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
                 node["ssh"], "pkill -f lms; sleep 2; nohup lms server start --port 1234 &"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            return True
    except:
        pass
    return False

def publish_node_event(name, status, models=None):
    payload = json.dumps({
        "type": f"node_{status}",
        "node": name,
        "models": models or [],
        "ts": datetime.now().isoformat()[:19]
    })
    r.publish("jarvis:events", payload)
    r.set(f"jarvis:node:{name}:status", status)
    r.set(f"jarvis:node:{name}:ts", datetime.now().isoformat()[:19])
    if models:
        r.set(f"jarvis:node:{name}:models", json.dumps(models))

prev_status = {}

print(f"[NodeWatchdog] Starting — monitoring M1/M2/M3")
while True:
    for name, node in NODES.items():
        up, models = check_node(name, node)
        prev = prev_status.get(name)

        if up and prev != "up":
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ {name} UP — {models[:2]}")
            publish_node_event(name, "up", models)
            prev_status[name] = "up"
        elif not up and prev != "down":
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ {name} DOWN")
            publish_node_event(name, "down")
            # Tenter SSH restart si node est M1 ou M3
            if name in ("M1", "M3"):
                if try_ssh_start_lmstudio(node):
                    print(f"  → SSH restart envoyé à {name}")
            prev_status[name] = "down"
        else:
            r.set(f"jarvis:node:{name}:status", "up" if up else "down")
            r.expire(f"jarvis:node:{name}:status", 120)

    time.sleep(30)
