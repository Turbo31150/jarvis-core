#!/usr/bin/env python3
"""JARVIS Agent Supervisor — Monitor agent processes, restart on crash"""
import subprocess, time, redis, json, os
from datetime import datetime

r = redis.Redis(decode_responses=True)

AGENTS = {
    "hw-monitor":     "python3 /home/turbo/jarvis/core/jarvis_hw_monitor.py",
    "llm-monitor":    "python3 /home/turbo/jarvis/core/jarvis_llm_monitor.py",
    "score-updater":  "python3 /home/turbo/jarvis/core/jarvis_score.py --daemon",
    "dashboard":      "python3 /home/turbo/jarvis/core/jarvis_dashboard_web.py",
}

_procs: dict[str, subprocess.Popen] = {}

def is_alive(name: str) -> bool:
    p = _procs.get(name)
    return p is not None and p.poll() is None

def start(name: str):
    cmd = AGENTS[name]
    p = subprocess.Popen(cmd.split(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _procs[name] = p
    r.hset("jarvis:supervisor", name, json.dumps({"pid": p.pid, "started": datetime.now().isoformat()[:19], "restarts": int(r.hget("jarvis:supervisor:restarts", name) or 0)}))
    print(f"[Supervisor] Started {name} PID={p.pid}")

def supervise_loop():
    for name in AGENTS:
        start(name)

    while True:
        for name in AGENTS:
            if not is_alive(name):
                r.hincrby("jarvis:supervisor:restarts", name, 1)
                print(f"[Supervisor] {name} died — restarting")
                start(name)
        time.sleep(10)

def status() -> dict:
    return {k: {"alive": is_alive(k), "pid": _procs[k].pid if _procs.get(k) else None}
            for k in AGENTS}

if __name__ == "__main__":
    print("[Supervisor] Starting agent supervision...")
    supervise_loop()
