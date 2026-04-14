#!/usr/bin/env python3
"""Sync n8n workflows into jarvis.db workflows table.

Primary source: n8n SQLite DB (~/.n8n/database.sqlite)
Fallback: n8n REST API (requires valid API key)
"""

import json
import sqlite3
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

N8N_API_URL = "http://127.0.0.1:5678/api/v1/workflows"
N8N_API_KEY = "n8n_api_1871ff55481de903ae3611f93dbb08abac156aa706fb1d37"
N8N_DB_PATH = Path.home() / ".n8n" / "database.sqlite"
JARVIS_DB_PATH = "/home/turbo/IA/Core/jarvis/data/jarvis.db"


def infer_trigger_type(nodes) -> str:
    if isinstance(nodes, str):
        try:
            nodes = json.loads(nodes)
        except Exception:
            return "manual"
    for node in nodes or []:
        t = node.get("type", "").lower()
        if "scheduletrigger" in t or "cron" in t or "interval" in t:
            return "cron"
        if "webhook" in t:
            return "webhook"
    return "manual"


def fetch_from_n8n_db() -> list:
    """Read directly from n8n's own SQLite database."""
    conn = sqlite3.connect(str(N8N_DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, name, active, nodes, connections, settings, staticData, "
        "pinData, versionId, meta, createdAt, updatedAt, isArchived "
        "FROM workflow_entity"
    ).fetchall()
    conn.close()
    workflows = []
    for r in rows:
        wf = dict(r)
        # Parse JSON fields
        for field in (
            "nodes",
            "connections",
            "settings",
            "staticData",
            "pinData",
            "meta",
        ):
            if isinstance(wf.get(field), str):
                try:
                    wf[field] = json.loads(wf[field])
                except Exception:
                    pass
        workflows.append(wf)
    return workflows


def fetch_from_api() -> list:
    """Fallback: fetch from n8n REST API."""
    req = urllib.request.Request(
        N8N_API_URL,
        headers={"X-N8N-API-KEY": N8N_API_KEY, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    return data.get("data", data) if isinstance(data, dict) else data


def ensure_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS workflows (
            id TEXT PRIMARY KEY,
            name TEXT,
            source TEXT,
            status TEXT,
            definition TEXT,
            trigger_type TEXT,
            updated_at TEXT
        )
    """)
    conn.commit()


def upsert_workflows(conn: sqlite3.Connection, workflows: list) -> tuple:
    now = datetime.now(timezone.utc).isoformat()
    inserted = updated = 0
    for wf in workflows:
        wf_id = str(wf.get("id", ""))
        name = wf.get("name", "")
        active = wf.get("active", False)
        # isArchived workflows are treated as inactive regardless of active flag
        archived = wf.get("isArchived", False)
        status = "active" if (active and not archived) else "inactive"
        definition = json.dumps(wf, default=str)
        trigger_type = infer_trigger_type(wf.get("nodes", []))

        existing = conn.execute(
            "SELECT id FROM workflows WHERE id = ?", (wf_id,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE workflows SET name=?, source=?, status=?, definition=?, "
                "trigger_type=?, updated_at=? WHERE id=?",
                (name, "n8n", status, definition, trigger_type, now, wf_id),
            )
            updated += 1
        else:
            conn.execute(
                "INSERT INTO workflows (id, name, source, status, definition, "
                "trigger_type, updated_at) VALUES (?,?,?,?,?,?,?)",
                (wf_id, name, "n8n", status, definition, trigger_type, now),
            )
            inserted += 1
    conn.commit()
    return inserted, updated


def main():
    # Try primary source: n8n SQLite
    if N8N_DB_PATH.exists():
        print(f"Source: n8n SQLite ({N8N_DB_PATH})")
        workflows = fetch_from_n8n_db()
    else:
        print("Source: n8n REST API (SQLite not found)")
        workflows = fetch_from_api()

    print(f"  → {len(workflows)} workflows fetched")

    conn = sqlite3.connect(JARVIS_DB_PATH)
    ensure_table(conn)
    inserted, updated = upsert_workflows(conn, workflows)
    conn.close()

    print(f"  → inserted={inserted}, updated={updated}")

    # Summary
    conn2 = sqlite3.connect(JARVIS_DB_PATH)
    rows = conn2.execute(
        "SELECT id, name, status, trigger_type FROM workflows WHERE source='n8n' ORDER BY name"
    ).fetchall()
    conn2.close()
    print(f"\nTotal in DB (source=n8n): {len(rows)}")
    active = sum(1 for r in rows if r[2] == "active")
    by_trigger = {}
    for r in rows:
        by_trigger[r[3]] = by_trigger.get(r[3], 0) + 1
    print(f"  active={active}, inactive={len(rows) - active}")
    print(f"  trigger breakdown: {by_trigger}")
    print("\nWorkflows:")
    for r in rows:
        print(f"  [{r[2]:8}] [{r[3]:7}] {r[1]}")


if __name__ == "__main__":
    main()
