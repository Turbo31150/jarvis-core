#!/usr/bin/env python3
"""Script d'installation complète OpenClaw."""
import os, subprocess, sys, shutil, json

def run(cmd):
    print(f"$ {cmd}")
    try:
        subprocess.run(cmd, shell=True, check=True)
    except Exception as e:
        print(f"ERR: {e}\nContinue...")

# 1. DÃ©pendances Python
print("=== DÃ©pendances Python ===")
reqs = ["requests", "beautifulsoup4", "rich", "pygments", "flask", "redis", 
        "chromadb", "mcp", "playwright", "PyGithub"]
for r in reqs:
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", r])
        print(f"Â± {r}")
    except:
        print(f"ERR: {r} (skip)")

# 2. RÃ©pertoires
print("\n=== RÃ©pertoires ===")
dirs = [
    "~/.openclaw/{docs,memory,commands,tools,ux,security,perf,hooks,history,logs,backups,integrations,orchestration,projects}",
    "~/IA/Core/jarvis/mcp-servers/{jarvis-cluster,jarvis-memory,jarvis-tools,jarvis-agents,jarvis-trading,jarvis-voice}",
    "~/Backups/openclaw"
]
for d in dirs:
    p = os.path.expandvars(d.replace("~", os.path.expanduser("~")))
    for sub in ["docs","memory","commands","tools","ux","security","perf","hooks","history","logs","backups","integrations","orchestration","projects"]:
        os.makedirs(os.path.join(p,sub), exist_ok=True)

for mc in ["jarvis-cluster","jarvis-memory","jarvis-tools","jarvis-agents","jarvis-trading","jarvis-voice"]:
    os.makedirs(f"{p}/mcp-servers/{mc}", exist_ok=True)

# 3. Services systemd
print("\n=== Services systemd ===")
for svc in ["lmstudio-server","openclaw-gateway","jarvis-docker","jarvis-proactive-monitor"]:
    state = subprocess.run(["systemctl", "--user", "is-active", svc], capture_output=True, text=True)
    status = "OK" if "active" in state.stdout else "NOT RUNNING"
    print(f"{status}: {svc}")

# 4. Connexion cluster
print("\n=== Cluster ===")
for n, url in [("M1","http://192.168.1.85:1234"),("M2","http://192.168.1.26:1234"),("OL1","http://127.0.0.1:11434")]:
    try:
        import requests
        r = requests.get(f"{url}/v1/models", timeout=3)
        data = r.json()
        count = len(data.get("data",[])) or len(data.get("models",[])) or 0
        print(f"{n}: {count} modÃ¨les")
    except Exception as e:
        print(f"{n}: DOWN ({str(e)[:30]})")

print("\n=== Installation terminÃ©e ===")
print("Lancer: openclaw pour dÃ©marrer")
