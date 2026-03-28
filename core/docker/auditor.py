"""JARVIS Docker Auditor — inventory, health, cleanup."""
import subprocess, json, re

def _run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    return r.stdout.strip()

def list_containers():
    out = _run(["docker","ps","--format","json"])
    return [json.loads(line) for line in out.split('\n') if line.strip()]

def container_stats():
    out = _run(["docker","stats","--no-stream","--format","json"])
    return [json.loads(line) for line in out.split('\n') if line.strip()]

def detect_crash_loops():
    containers = list_containers()
    loops = []
    for c in containers:
        if "restarting" in c.get("Status","").lower():
            loops.append(c)
    return loops

def unused_images():
    out = _run(["docker","images","--format","json","--filter","dangling=true"])
    return [json.loads(line) for line in out.split('\n') if line.strip()]

def orphan_volumes():
    out = _run(["docker","volume","ls","--format","json","--filter","dangling=true"])
    return [json.loads(line) for line in out.split('\n') if line.strip()]

def recent_logs(name, lines=20):
    return _run(["docker","logs","--tail",str(lines),name])

def snapshot():
    return {
        "containers": list_containers(),
        "crash_loops": detect_crash_loops(),
        "unused_images": len(unused_images()),
        "orphan_volumes": len(orphan_volumes()),
    }

if __name__ == "__main__":
    print(json.dumps(snapshot(), indent=2, default=str))
