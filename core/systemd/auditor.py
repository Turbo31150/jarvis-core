"""JARVIS Systemd Auditor — inventory, validation, sync."""
import subprocess, re, os
from pathlib import Path

USER_UNITS = Path.home() / ".config/systemd/user"
SYSTEM_UNITS = Path("/etc/systemd/system")

def list_user_units():
    r = subprocess.run(["systemctl","--user","list-units","--type=service","--all","--no-pager"],
        capture_output=True, text=True, timeout=10)
    units = []
    for line in r.stdout.split('\n')[1:]:
        parts = line.split()
        if len(parts) >= 4 and '.service' in parts[0]:
            units.append({"name": parts[0], "load": parts[1], "active": parts[2], "sub": parts[3]})
    return units

def detect_stale_paths(old_path="~/jarvis-linux"):
    stale = []
    for f in USER_UNITS.glob("*.service"):
        content = f.read_text()
        if old_path in content:
            stale.append({"file": str(f), "old_path": old_path})
    return stale

def detect_missing_restart():
    missing = []
    for f in USER_UNITS.glob("*.service"):
        content = f.read_text()
        if "[Service]" in content and "RestartSec" not in content:
            missing.append(str(f.name))
    return missing

def detect_port_mismatch(expected_ports):
    mismatches = []
    for f in USER_UNITS.glob("*.service"):
        content = f.read_text()
        for port, desc in expected_ports.items():
            if str(port) in content:
                from core.network.health import check_port
                if not check_port("127.0.0.1", port):
                    mismatches.append({"unit": f.name, "port": port, "desc": desc, "listening": False})
    return mismatches

def full_report():
    return {
        "user_units": list_user_units(),
        "stale_paths": detect_stale_paths(),
        "missing_restart": detect_missing_restart(),
    }

if __name__ == "__main__":
    import json
    print(json.dumps(full_report(), indent=2))
