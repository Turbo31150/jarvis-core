#!/usr/bin/env python3
"""
sync_config.py — Synchronise l'état Docker + LLM backends → jarvis.db
Modes:
  once    : snapshot unique (cron/systemd)
  daemon  : écoute events Docker en continu
"""

import argparse
import json
import logging
import sqlite3
from pathlib import Path

import requests

DB_PATH = Path.home() / "IA/Core/jarvis/data/jarvis.db"
LLM_NODES = [
    {"node": "M1", "address": "http://127.0.0.1:1234", "priority": 1},
    {"node": "M2", "address": "http://192.168.1.26:1234", "priority": 2},
    {"node": "OL1", "address": "http://127.0.0.1:11434", "priority": 3},
    {"node": "CLOUD", "address": "https://ollama.com/v1", "priority": 4},
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("sync_config")


def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def sync_containers():
    """Sync running Docker containers → containers table."""
    try:
        import docker

        client = docker.from_env()
    except Exception as e:
        log.warning(f"Docker SDK unavailable: {e}")
        return

    with db_conn() as conn:
        # Mark all stopped first
        conn.execute(
            "UPDATE containers SET status='stopped', updated_at=datetime('now')"
        )

        for c in client.containers.list(all=True):
            attrs = c.attrs
            ports_raw = attrs.get("HostConfig", {}).get("PortBindings") or {}
            ports = [f"{k}:{v[0]['HostPort']}" for k, v in ports_raw.items() if v]
            volumes = attrs.get("HostConfig", {}).get("Binds") or []
            networks = list(
                (attrs.get("NetworkSettings", {}).get("Networks") or {}).keys()
            )
            restart = (attrs.get("HostConfig", {}).get("RestartPolicy") or {}).get(
                "Name", "no"
            )

            conn.execute(
                """INSERT OR REPLACE INTO containers
                   (id, name, image, status, ports, volumes, networks, restart_policy, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,datetime('now'))""",
                (
                    c.name,
                    c.name,
                    c.image.tags[0] if c.image.tags else c.image.short_id,
                    c.status,
                    json.dumps(ports),
                    json.dumps(volumes),
                    json.dumps(networks),
                    restart,
                ),
            )

    log.info("Containers synced")


def _probe_lm_models(address: str, node: str) -> list[str]:
    """Returns model list from LMStudio or Ollama."""
    try:
        if node == "OL1":
            r = requests.get(f"{address}/api/tags", timeout=2)
            return [m["name"] for m in r.json().get("models", [])]
        elif node == "CLOUD":
            return ["qwen3-next:80b", "deepseek-v3.1:671b", "chatgpt-oss:120b"]
        else:
            r = requests.get(f"{address}/v1/models", timeout=2)
            return [m["id"] for m in r.json().get("data", [])]
    except Exception:
        return []


def sync_llm_backends():
    """Probe all LLM nodes and update llm_backends table."""
    with db_conn() as conn:
        for n in LLM_NODES:
            node, addr, prio = n["node"], n["address"], n["priority"]

            if node == "CLOUD":
                status = "up"
                models = _probe_lm_models(addr, node)
            else:
                try:
                    url = f"{addr}/api/tags" if node == "OL1" else f"{addr}/v1/models"
                    r = requests.get(url, timeout=2)
                    status = "up" if r.ok else "degraded"
                    models = _probe_lm_models(addr, node)
                except Exception:
                    status = "down"
                    models = []

            conn.execute(
                """INSERT INTO llm_backends
                   (node, address, models, status, priority, updated_at)
                   VALUES (?,?,?,?,?,datetime('now'))
                   ON CONFLICT(node) DO UPDATE SET
                     models=excluded.models, status=excluded.status,
                     updated_at=excluded.updated_at""",
                (node, addr, json.dumps(models), status, prio),
            )

    log.info("LLM backends synced")


def sync_orchestration():
    """Probe running orchestration components."""
    checks = [
        ("dispatcher", "http://127.0.0.1:9742/health"),
        ("n8n", "http://127.0.0.1:5678/healthz"),
        ("pipeline", "http://127.0.0.1:9742/"),
    ]
    with db_conn() as conn:
        for component, url in checks:
            try:
                r = requests.get(url, timeout=2)
                state = "running" if r.ok else "error"
            except Exception:
                state = "stopped"

            conn.execute(
                """UPDATE orchestration_state SET state=?, last_heartbeat=datetime('now')
                   WHERE component=?""",
                (state, component),
            )


def run_once():
    sync_containers()
    sync_llm_backends()
    sync_orchestration()
    log.info("Snapshot complete")


def run_daemon():
    """Listen to Docker events and sync on container start/stop/die."""
    log.info("Daemon mode — listening Docker events")

    # Initial snapshot
    run_once()

    try:
        import docker

        client = docker.from_env()
    except Exception as e:
        log.error(f"Docker SDK required for daemon mode: {e}")
        return

    for event in client.events(decode=True, filters={"type": "container"}):
        action = event.get("Action", "")
        name = (event.get("Actor", {}).get("Attributes") or {}).get("name", "?")
        if action in ("start", "stop", "die", "destroy", "create"):
            log.info(f"Docker event: {action} → {name}")
            sync_containers()
            sync_orchestration()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync Docker/LLM config → jarvis.db")
    parser.add_argument("mode", choices=["once", "daemon"], default="once", nargs="?")
    args = parser.parse_args()

    if args.mode == "daemon":
        run_daemon()
    else:
        run_once()
