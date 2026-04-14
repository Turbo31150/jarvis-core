"""JARVIS Workflows — Morning startup, end of day, incident triage, reviews."""

import time
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.services import ServiceRegistry
from core.network.health import full_report as network_report
from core.memory.facade import MemoryFacade


def morning_startup():
    """Morning routine — check everything."""
    results = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"), "checks": {}}

    # 1. Cluster health
    reg = ServiceRegistry()
    health = reg.health_check_all()
    results["checks"]["cluster"] = {k: "UP" if v else "DOWN" for k, v in health.items()}

    # 2. Network
    net = network_report()
    up = sum(1 for s in net["services"].values() if s["up"])
    results["checks"]["network"] = f"{up}/{len(net['services'])} services up"

    # 3. DB
    try:
        mem = MemoryFacade()
        stats = mem.get_stats()
        results["checks"]["db"] = {
            db: f"{s.get('tables', 0)} tables" for db, s in stats.items()
        }
    except:
        results["checks"]["db"] = "error"

    # 4. BrowserOS
    try:
        from core.browseros_workflows import morning_startup as bos_start

        bos = bos_start()
        results["checks"]["browseros"] = bos
    except:
        results["checks"]["browseros"] = "not available"

    return results


def end_of_day():
    """EOD report — summarize the day."""
    import sqlite3

    conn = sqlite3.connect("data/jarvis-master.db")
    c = conn.cursor()

    today = time.strftime("%Y-%m-%d")
    offers = c.execute("SELECT COUNT(*), SUM(amount) FROM codeur_offers").fetchone()
    runs = c.execute(
        "SELECT COUNT(*) FROM workflow_runs WHERE timestamp LIKE ?", (today + "%",)
    ).fetchone()[0]
    actions = c.execute(
        "SELECT COUNT(*) FROM linkedin_actions WHERE timestamp LIKE ?", (today + "%",)
    ).fetchone()[0]
    conn.close()

    return {
        "date": today,
        "codeur_offers": {"count": offers[0], "total_eur": offers[1]},
        "workflow_runs_today": runs,
        "linkedin_actions_today": actions,
    }


def incident_triage():
    """Check for incidents — failed services, DB issues, network problems."""
    incidents = []

    # Check services
    reg = ServiceRegistry()
    for name, up in reg.health_check_all().items():
        if not up:
            incidents.append({"type": "service_down", "name": name, "severity": "high"})

    # Check /tmp
    import shutil

    tmp_usage = shutil.disk_usage("/tmp")
    if tmp_usage.used / tmp_usage.total > 0.8:
        incidents.append(
            {
                "type": "disk_full",
                "path": "/tmp",
                "usage": f"{tmp_usage.used / tmp_usage.total:.0%}",
                "severity": "critical",
            }
        )

    # Check GPU temps
    import subprocess

    try:
        r = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for i, temp in enumerate(r.stdout.strip().split("\n")):
            try:
                if int(temp) > 85:
                    incidents.append(
                        {
                            "type": "gpu_hot",
                            "gpu": i,
                            "temp": int(temp),
                            "severity": "critical",
                        }
                    )
            except:
                pass
    except:
        pass

    return incidents


def trading_check():
    """Trading workflow — LLM status + system score pour signaux."""
    import redis
    import json

    r = redis.Redis(decode_responses=True)
    result = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "llm": {}}
    for key in r.scan_iter("jarvis:llm:m*"):
        name = key.replace("jarvis:llm:", "")
        d = json.loads(r.get(key) or "{}")
        result["llm"][name] = {"ok": d.get("ok"), "latency_ms": d.get("latency_ms")}
    score = json.loads(r.get("jarvis:score") or "{}")
    result["system_score"] = score.get("total", 0)
    result["m2_up"] = score.get("m2_up", False)
    return result


def full_health_snapshot():
    """Snapshot complet — score + GPUs + nodes + LLM."""
    import redis
    import json

    r = redis.Redis(decode_responses=True)
    score = json.loads(r.get("jarvis:score") or "{}")
    gpus = {
        f"gpu{i}": {
            "temp": r.get(f"jarvis:gpu:{i}:temp"),
            "vram_pct": r.get(f"jarvis:gpu:{i}:vram_pct"),
        }
        for i in range(5)
    }
    nodes = {
        n: r.get(f"jarvis:node:{n}:status") or "unknown" for n in ["M1", "M2", "M3"]
    }
    return {"score": score, "gpus": gpus, "nodes": nodes}


def github_review():
    """Review GitHub changes."""
    from core.github_audit import daily_summary

    return daily_summary()


def network_review():
    """Network health review."""
    return network_report()


if __name__ == "__main__":
    print("=== Morning Startup ===")
    print(json.dumps(morning_startup(), indent=2, default=str))
    print("\n=== Incident Triage ===")
    print(json.dumps(incident_triage(), indent=2))
