"""JARVIS Network Health — ping, ports, DNS, gateway, latency."""
import socket, subprocess, time, json, os
from pathlib import Path
from typing import Optional

SERVICES = {
    "M1_LMStudio": ("127.0.0.1", 1234),
    "M2_LMStudio": ("192.168.1.26", 1234),
    "M3_LMStudio": ("192.168.1.113", 1234),
    "OL1_Ollama": ("127.0.0.1", 11434),
    "BrowserOS_CDP": ("127.0.0.1", 9108),
    "BrowserOS_MCP": ("127.0.0.1", 9001),
    "BrowserOS_Server": ("127.0.0.1", 9200),
    "Dashboard": ("127.0.0.1", 8888),
}

def check_port(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except: return False

def check_dns(host: str = "google.com") -> bool:
    try:
        socket.getaddrinfo(host, 443)
        return True
    except: return False

def check_gateway() -> Optional[str]:
    try:
        r = subprocess.run(["ip", "route", "show", "default"], capture_output=True, text=True, timeout=5)
        for line in r.stdout.split('\n'):
            if 'default' in line:
                return line.split()[2]
    except: pass
    return None

def ping(host: str, count: int = 1, timeout: int = 3) -> Optional[float]:
    try:
        r = subprocess.run(["ping", "-c", str(count), "-W", str(timeout), host],
            capture_output=True, text=True, timeout=timeout+2)
        for line in r.stdout.split('\n'):
            if 'avg' in line:
                return float(line.split('/')[-3])
    except: pass
    return None

def port_scan() -> dict:
    results = {}
    for name, (host, port) in SERVICES.items():
        results[name] = {"host": host, "port": port, "up": check_port(host, port)}
    return results

def latency_map() -> dict:
    hosts = {"M1": "127.0.0.1", "M2": "192.168.1.26", "M3": "192.168.1.113"}
    return {name: ping(host) for name, host in hosts.items()}

def full_report() -> dict:
    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "services": port_scan(),
        "dns": check_dns(),
        "gateway": check_gateway(),
        "latency": latency_map(),
    }

def conflict_detect() -> list:
    """Detect port conflicts."""
    conflicts = []
    r = subprocess.run(["ss", "-tlnp"], capture_output=True, text=True, timeout=5)
    ports = {}
    for line in r.stdout.split('\n')[1:]:
        parts = line.split()
        if len(parts) >= 6:
            addr = parts[3]
            proc = parts[5] if len(parts) > 5 else ""
            port = addr.split(':')[-1]
            if port in ports and ports[port] != proc:
                conflicts.append({"port": port, "proc1": ports[port], "proc2": proc})
            ports[port] = proc
    return conflicts

if __name__ == "__main__":
    import json
    print(json.dumps(full_report(), indent=2))
