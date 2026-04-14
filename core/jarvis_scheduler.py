#!/usr/bin/env python3
"""JARVIS Scheduler — Tâches planifiées + réactives (SQLite-backed)"""

import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

DB = Path("/home/turbo/jarvis/core/scheduler.db")


def init_db():
    con = sqlite3.connect(DB)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        schedule TEXT,        -- cron-like: 'every_30m' | 'daily_03h' | 'on_event:gpu_critical'
        agent TEXT,           -- openclaw agent ou script
        prompt TEXT,
        last_run TEXT,
        next_run TEXT,
        enabled INTEGER DEFAULT 1,
        run_count INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER,
        started_at TEXT,
        finished_at TEXT,
        status TEXT,
        output TEXT
    );
    """)
    # Seed tâches par défaut
    tasks = [
        (
            "llm_monitor",
            "every_5m",
            None,
            "python3 /home/turbo/jarvis/core/jarvis_llm_monitor.py",
            1,
        ),
        (
            "daily_bench",
            "daily_03h",
            None,
            "python3 /home/turbo/jarvis/core/benchmark_daily.py",
            1,
        ),
        (
            "score_update",
            "every_30s",
            None,
            "python3 /home/turbo/jarvis/core/jarvis_score.py",
            1,
        ),
        (
            "node_watchdog",
            "every_30s",
            None,
            "python3 /home/turbo/jarvis/core/jarvis_node_watchdog.py",
            0,
        ),
    ]
    for name, sched, agent, prompt, enabled in tasks:
        con.execute(
            "INSERT OR IGNORE INTO tasks(name,schedule,agent,prompt,enabled) VALUES(?,?,?,?,?)",
            (name, sched, agent, prompt, enabled),
        )
    con.commit()
    con.close()


def list_tasks():
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT id,name,schedule,enabled,run_count,last_run FROM tasks"
    ).fetchall()
    con.close()
    return rows


def run_task(task_id: int):
    con = sqlite3.connect(DB)
    row = con.execute(
        "SELECT name,prompt,agent FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    if not row:
        return
    name, cmd, agent = row
    started = datetime.now().isoformat()
    try:
        # SEC-003: commande issue DB — shell=True supprimé pour éviter RCE
        result = subprocess.run(
            shlex.split(cmd), capture_output=True, text=True, timeout=120
        )
        out = result.stdout[-500:] if result.stdout else result.stderr[-200:]
        status = "ok" if result.returncode == 0 else "error"
    except Exception as e:
        out = str(e)
        status = "exception"
    finished = datetime.now().isoformat()
    con.execute(
        "INSERT INTO runs(task_id,started_at,finished_at,status,output) VALUES(?,?,?,?,?)",
        (task_id, started, finished, status, out),
    )
    con.execute(
        "UPDATE tasks SET last_run=?,run_count=run_count+1 WHERE id=?",
        (finished, task_id),
    )
    con.commit()
    con.close()
    return status


if __name__ == "__main__":
    init_db()
    print("Scheduler DB initialisé:")
    for row in list_tasks():
        print(f"  #{row[0]} {row[1]:20} {row[2]:15} enabled={row[3]} runs={row[4]}")
