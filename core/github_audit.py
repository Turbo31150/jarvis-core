"""JARVIS GitHub Audit — detect issues in codebase."""
import subprocess, re, os
from pathlib import Path

BASE = Path(os.getenv("JARVIS_BASE", os.path.expanduser("~/IA/Core/jarvis")))

def scan_todos():
    r = subprocess.run(["grep","-rn","TODO\\|FIXME\\|HACK\\|XXX","--include=*.py","--include=*.sh",str(BASE)],
        capture_output=True, text=True, timeout=10)
    return [line for line in r.stdout.split('\n') if line.strip()]

def scan_hardcoded_ports():
    r = subprocess.run(["grep","-rn","127\\.0\\.0\\.1:\\|192\\.168\\.","--include=*.py","--include=*.sh",str(BASE)],
        capture_output=True, text=True, timeout=10)
    return [line for line in r.stdout.split('\n') if line.strip() and 'config' not in line.lower()]

def scan_broken_imports():
    broken = []
    for f in BASE.rglob("*.py"):
        if '__pycache__' in str(f): continue
        try:
            content = f.read_text()
            for line in content.split('\n'):
                if line.startswith('import ') or line.startswith('from '):
                    pass  # Could validate each import
        except: pass
    return broken

def scripts_without_tests():
    scripts = {f.stem for f in (BASE/"scripts").glob("*.py")}
    tests = {f.stem.replace("test_","") for f in (BASE/"tests").glob("test_*.py")}
    return scripts - tests

def daily_summary():
    return {
        "todos": len(scan_todos()),
        "hardcoded_ports": len(scan_hardcoded_ports()),
        "scripts_without_tests": list(scripts_without_tests())[:10],
    }

if __name__ == "__main__":
    import json
    print(json.dumps(daily_summary(), indent=2))
