"""JARVIS Unified Observability — Health snapshots, service/agent maps, anomaly detection."""
import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "jarvis-master.db"

SEVERITIES = ("info", "warning", "error", "critical")


@dataclass
class Event:
    event_type: str
    source: str
    severity: str = "info"  # info | warning | error | critical
    payload: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self):
        if self.severity not in SEVERITIES:
            self.severity = "info"


def _query(sql: str, params: tuple = (), db: Path = _DB_PATH) -> List[tuple]:
    try:
        with sqlite3.connect(str(db)) as conn:
            return conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []


def health_snapshot(db: Path = _DB_PATH) -> Dict[str, Any]:
    """Full system state: cluster, tasks, DB size, recent errors."""
    snap: Dict[str, Any] = {"timestamp": datetime.now(tz=None).isoformat()}

    # Cluster health (latest row)
    rows = _query(
        "SELECT * FROM cluster_health ORDER BY timestamp DESC LIMIT 1", db=db
    )
    if rows:
        r = rows[0]
        snap["cluster"] = {
            "timestamp": r[0],
            "m1": {"status": r[1], "models": r[2]},
            "m2": {"status": r[3], "models": r[4]},
            "m3": {"status": r[5], "models": r[6]},
            "ol1": {"status": r[7]},
            "gpu_temps": r[8],
            "vram_used": r[9],
        }

    # Task queue summary
    rows = _query(
        "SELECT status, COUNT(*) FROM task_queue GROUP BY status", db=db
    )
    snap["tasks"] = {r[0]: r[1] for r in rows}

    # DB file size
    if db.is_file():
        snap["db_size_mb"] = round(db.stat().st_size / (1024 * 1024), 2)

    # Recent failures from events table
    cutoff = time.time() - 3600
    rows = _query(
        "SELECT COUNT(*) FROM events WHERE event_type LIKE '%.failed' "
        "AND timestamp > ?", (cutoff,), db=db,
    )
    snap["failures_last_hour"] = rows[0][0] if rows else 0

    return snap


def service_map() -> Dict[str, Dict[str, Any]]:
    """Map of all known services and their dependencies."""
    return {
        "m1-lmstudio": {"host": "127.0.0.1", "port": 1234,
                         "models": ["gemma-3-4b", "qwen3.5-9b", "deepseek-r1"],
                         "depends_on": []},
        "m2-lmstudio": {"host": "192.168.1.26", "port": 1234,
                         "models": ["deepseek-coder"], "depends_on": ["network"]},
        "m3-lmstudio": {"host": "192.168.1.113", "port": 1234,
                         "models": ["deepseek-r1-qwen3-8b"], "depends_on": ["network"]},
        "ol1-ollama":   {"host": "127.0.0.1", "port": 11434,
                         "models": ["qwen2.5:1.5b", "deepseek-r1:7b"],
                         "depends_on": []},
        "browseros":    {"host": "127.0.0.1", "port": 3025,
                         "depends_on": ["chrome"]},
        "telegram":     {"type": "bot", "depends_on": ["network"]},
    }


def agent_map(db: Path = _DB_PATH) -> Dict[str, Any]:
    """Map of registered agents from events table."""
    rows = _query(
        "SELECT source, payload FROM events WHERE event_type = 'agent.registered' "
        "ORDER BY timestamp DESC", db=db,
    )
    agents: Dict[str, Any] = {}
    for source, payload_str in rows:
        try:
            payload = json.loads(payload_str) if payload_str else {}
        except (json.JSONDecodeError, TypeError):
            payload = {}
        agents[source] = payload
    return agents


def recent_failures(hours: int = 24, db: Path = _DB_PATH) -> List[Dict[str, Any]]:
    """List of recent error events within the given timeframe."""
    cutoff = time.time() - (hours * 3600)
    rows = _query(
        "SELECT event_type, source, payload, timestamp FROM events "
        "WHERE event_type LIKE '%.failed' AND timestamp > ? "
        "ORDER BY timestamp DESC LIMIT 100",
        (cutoff,), db=db,
    )
    results = []
    for etype, source, payload_str, ts in rows:
        try:
            payload = json.loads(payload_str) if payload_str else {}
        except (json.JSONDecodeError, TypeError):
            payload = {}
        results.append({
            "event_type": etype, "source": source,
            "payload": payload, "timestamp": ts,
        })
    return results


def anomalies(db: Path = _DB_PATH) -> Dict[str, List[str]]:
    """Detect DB and network anomalies."""
    issues: Dict[str, List[str]] = {"db": [], "network": []}

    # DB anomalies: check if DB exists and is not corrupt
    if not db.is_file():
        issues["db"].append("jarvis-master.db missing")
    else:
        try:
            with sqlite3.connect(str(db)) as conn:
                conn.execute("PRAGMA integrity_check")
        except sqlite3.Error as e:
            issues["db"].append(f"integrity error: {e}")

    # Network anomalies: check cluster health for offline nodes
    rows = _query(
        "SELECT m1_status, m2_status, m3_status, ol1_status "
        "FROM cluster_health ORDER BY timestamp DESC LIMIT 1", db=db,
    )
    if rows:
        labels = ["m1", "m2", "m3", "ol1"]
        for i, label in enumerate(labels):
            status = rows[0][i]
            if status and status.lower() not in ("online", "ok", "healthy"):
                issues["network"].append(f"{label}: {status}")

    return issues


if __name__ == "__main__":
    print("=== Health Snapshot ===")
    snap = health_snapshot()
    for k, v in snap.items():
        print(f"  {k}: {v}")

    print("\n=== Service Map ===")
    for name, info in service_map().items():
        print(f"  {name}: {info.get('host', 'n/a')}:{info.get('port', 'n/a')}")

    print("\n=== Agent Map ===")
    agents = agent_map()
    print(f"  {len(agents)} agent(s) registered")

    print("\n=== Anomalies ===")
    for category, items in anomalies().items():
        print(f"  {category}: {items if items else 'none'}")
