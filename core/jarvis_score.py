#!/usr/bin/env python3
"""JARVIS System Score /100 — Calcul temps réel"""
import subprocess, json, time, sqlite3
from datetime import datetime

def get_score() -> dict:
    scores = {}
    
    # CPU (20pts): temp < 70° = 20, < 80° = 10, else 0
    try:
        out = subprocess.check_output(["sensors"], text=True, timeout=3)
        import re
        t = float(re.search(r'Tctl:\s+\+(\d+\.\d+)', out).group(1))
        scores["cpu_thermal"] = 20 if t < 70 else (10 if t < 80 else 0)
        scores["cpu_temp"] = t
    except:
        scores["cpu_thermal"] = 10; scores["cpu_temp"] = 0

    # RAM (20pts): free > 10GB = 20, > 4GB = 10, else 5
    try:
        with open("/proc/meminfo") as f:
            lines = {l.split(':')[0]: int(l.split()[1]) for l in f if ':' in l}
        free_gb = (lines.get("MemAvailable",0)) / 1024 / 1024
        scores["ram"] = 20 if free_gb > 10 else (10 if free_gb > 4 else 5)
        scores["ram_free_gb"] = round(free_gb, 1)
    except:
        scores["ram"] = 10

    # GPU (20pts): tous GPU sains
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=temperature.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"], text=True, timeout=5)
        gpus = [list(map(int, line.split(','))) for line in out.strip().split('\n') if line.strip()]
        max_temp = max(g[0] for g in gpus)
        scores["gpu"] = 20 if max_temp < 75 else (10 if max_temp < 85 else 0)
        scores["gpu_max_temp"] = max_temp
    except:
        scores["gpu"] = 10

    # LLM backends (20pts): M2 + OL1
    import requests
    m2_ok = ol1_ok = False
    try:
        requests.get("http://192.168.1.26:1234/v1/models", timeout=2); m2_ok = True
    except: pass
    try:
        requests.get("http://127.0.0.1:11434/api/tags", timeout=2); ol1_ok = True
    except: pass
    scores["llm"] = (10 if m2_ok else 0) + (10 if ol1_ok else 0)
    scores["m2_up"] = m2_ok; scores["ol1_up"] = ol1_ok

    # Services (20pts): redis + health-patrol
    try:
        r = subprocess.run(["systemctl","is-active","redis-server"], capture_output=True, text=True)
        redis_ok = r.stdout.strip() == "active"
        r2 = subprocess.run(["systemctl","is-active","health-patrol"], capture_output=True, text=True)
        patrol_ok = r2.stdout.strip() == "active"
        scores["services"] = (10 if redis_ok else 0) + (10 if patrol_ok else 0)
    except:
        scores["services"] = 0

    total = scores["cpu_thermal"] + scores["ram"] + scores["gpu"] + scores["llm"] + scores["services"]
    return {"total": total, "ts": datetime.now().isoformat()[:19], **scores}

if __name__ == "__main__":
    s = get_score()
    print(f"\n{'='*40}")
    print(f"  JARVIS Score: {s['total']}/100  [{s['ts']}]")
    print(f"{'='*40}")
    print(f"  CPU:      {s['cpu_thermal']}/20  (T={s.get('cpu_temp',0)}°C)")
    print(f"  RAM:      {s['ram']}/20  ({s.get('ram_free_gb',0)}GB libre)")
    print(f"  GPU:      {s['gpu']}/20  (max={s.get('gpu_max_temp',0)}°C)")
    print(f"  LLM:      {s['llm']}/20  (M2={'✅' if s.get('m2_up') else '❌'} OL1={'✅' if s.get('ol1_up') else '❌'})")
    print(f"  Services: {s['services']}/20  (Redis+Patrol)")
    print(f"{'='*40}\n")
    
    # Store in Redis
    try:
        import redis
        rdb = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
        rdb.setex("jarvis:score", 300, json.dumps(s))
    except: pass
