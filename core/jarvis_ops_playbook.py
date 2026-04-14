#!/usr/bin/env python3
"""JARVIS Ops Playbook — Automated runbooks for common operational incidents"""
import subprocess, redis, json
from datetime import datetime

r = redis.Redis(decode_responses=True)

PLAYBOOKS = {
    "high_gpu_temp": {
        "description": "GPU temp > 82°C",
        "steps": [
            ("check", "nvidia-smi --query-gpu=index,temperature.gpu,power.draw --format=csv,noheader"),
            ("reduce_power", "nvidia-smi -i 0 -pl 150"),
            ("verify", "nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader"),
        ]
    },
    "low_ram": {
        "description": "RAM free < 2GB",
        "steps": [
            ("check", "free -h"),
            ("top_consumers", "ps aux --sort=-%mem | head -10"),
            ("drop_caches", "sync && echo 1 | sudo tee /proc/sys/vm/drop_caches"),
            ("verify", "free -h"),
        ]
    },
    "service_restart": {
        "description": "Restart a failed JARVIS service",
        "steps": [
            ("check_status", "sudo systemctl status jarvis-{service}.service --no-pager"),
            ("restart", "sudo systemctl restart jarvis-{service}.service"),
            ("verify", "sudo systemctl is-active jarvis-{service}.service"),
        ]
    },
    "redis_flush_old": {
        "description": "Clean old Redis keys",
        "steps": [
            ("count", "redis-cli dbsize"),
            ("flush_expired", "redis-cli --scan --pattern 'jarvis:ctx:*' | head -100 | xargs redis-cli del"),
        ]
    },
}

def run_playbook(name: str, context: dict = {}) -> dict:
    pb = PLAYBOOKS.get(name)
    if not pb:
        return {"error": f"unknown playbook: {name}"}
    
    results = {"playbook": name, "description": pb["description"],
               "ts": datetime.now().isoformat()[:19], "steps": []}
    
    for step_name, cmd in pb["steps"]:
        cmd_fmt = cmd.format(**context) if context else cmd
        try:
            out = subprocess.run(cmd_fmt, shell=True, capture_output=True, text=True, timeout=15)
            results["steps"].append({
                "step": step_name, "cmd": cmd_fmt,
                "ok": out.returncode == 0,
                "output": (out.stdout or out.stderr)[:200]
            })
        except Exception as e:
            results["steps"].append({"step": step_name, "ok": False, "output": str(e)})
    
    r.lpush("jarvis:playbook_runs", json.dumps(results))
    r.ltrim("jarvis:playbook_runs", 0, 49)
    return results

def list_playbooks() -> list:
    return [{"name": k, "description": v["description"], "steps": len(v["steps"])}
            for k, v in PLAYBOOKS.items()]

if __name__ == "__main__":
    print("Available playbooks:")
    for pb in list_playbooks():
        print(f"  {pb['name']}: {pb['description']} ({pb['steps']} steps)")
    
    print("\nRunning 'low_ram' playbook (dry-check)...")
    res = run_playbook("low_ram")
    for s in res["steps"]:
        status = "✅" if s["ok"] else "❌"
        print(f"  {status} {s['step']}: {s['output'][:60]}")
