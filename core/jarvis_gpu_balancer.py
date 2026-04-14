#!/usr/bin/env python3
"""JARVIS GPU Balancer — Route inference to least-loaded GPU"""
import redis, json, subprocess
from datetime import datetime

r = redis.Redis(decode_responses=True)

GPU_PORTS = {0: 11435, 1: 11436, 2: 11437, 3: 11438, 4: 11439}  # hypothetical Ollama instances

def get_gpu_load() -> dict:
    """Get VRAM % and temp for all GPUs"""
    loads = {}
    for i in range(5):
        vram = float(r.get(f"jarvis:gpu:{i}:vram_pct") or 100)
        temp = float(r.get(f"jarvis:gpu:{i}:temp") or 85)
        loads[i] = {"vram_pct": vram, "temp_c": temp, "score": vram * 0.7 + (temp/85) * 30}
    return loads

def best_gpu() -> int:
    """Return GPU with lowest load score"""
    loads = get_gpu_load()
    return min(loads, key=lambda i: loads[i]["score"])

def get_routing_recommendation() -> dict:
    loads = get_gpu_load()
    best = min(loads, key=lambda i: loads[i]["score"])
    worst = max(loads, key=lambda i: loads[i]["score"])
    return {
        "best_gpu": best,
        "worst_gpu": worst,
        "loads": loads,
        "ts": datetime.now().isoformat()[:19]
    }

def publish_routing():
    rec = get_routing_recommendation()
    r.setex("jarvis:gpu_routing", 60, json.dumps(rec))
    return rec

if __name__ == "__main__":
    rec = publish_routing()
    print(f"Best GPU: {rec['best_gpu']} | Worst: {rec['worst_gpu']}")
    for i, l in rec['loads'].items():
        print(f"  GPU{i}: VRAM={l['vram_pct']}% Temp={l['temp_c']}°C Score={l['score']:.1f}")
