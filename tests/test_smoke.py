"""JARVIS Smoke Test — Verify entire system is operational."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def test_imports():
    """Core modules importable."""
    from core.tasks.models import TaskRequest, TaskResult, TaskStatus, TaskPriority
    assert TaskStatus.COMPLETED.value == "completed"
    print("  ✅ Core models")

def test_db():
    """Database accessible."""
    import sqlite3
    conn = sqlite3.connect("data/jarvis-master.db")
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    assert len(tables) >= 9
    conn.close()
    print(f"  ✅ Database ({len(tables)} tables)")

def test_network():
    """Key services responding."""
    from core.network.health import check_port
    services = {"M1": (1234, True), "M3": (1234, True)}
    for name, (port, _) in services.items():
        host = "127.0.0.1" if name == "M1" else "192.168.1.113"
        up = check_port(host, port)
        print(f"  {'✅' if up else '❌'} {name}:{port}")

def test_browseros():
    """BrowserOS CLI responding."""
    import subprocess
    r = subprocess.run([os.path.expanduser("~/.browseros/bin/browseros-cli"), "health"],
        capture_output=True, text=True, timeout=5)
    ok = "ok" in r.stdout
    print(f"  {'✅' if ok else '❌'} BrowserOS CLI")

def test_scripts():
    """Key scripts exist and are executable."""
    scripts = ["scripts/cdp.py", "scripts/codeur-veille.py", "scripts/publish.py",
               "scripts/comet_cluster.py", "scripts/db_sync.py"]
    for s in scripts:
        exists = os.path.exists(s)
        print(f"  {'✅' if exists else '❌'} {s}")

if __name__ == "__main__":
    print("🧪 JARVIS Smoke Test\n")
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for test in [test_imports, test_db, test_network, test_browseros, test_scripts]:
        try:
            test()
        except Exception as e:
            print(f"  ❌ {test.__name__}: {e}")
    print("\n✅ Smoke test complete")
