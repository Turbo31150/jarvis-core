#!/usr/bin/env python3
"""Dispatcher tâches OpenClaw : exécute les tâches [OC-*] via agents OpenClaw."""
import sqlite3, subprocess, os, sys
from pathlib import Path
from datetime import datetime

DB = Path.home() / 'IA/Core/jarvis/data/jarvis-master.db'

def get_oc_tasks(limit=5):
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    conn = sqlite3.connect(str(DB))
    rows = conn.execute(
        "SELECT id, description, agent FROM openclaw_tasks "
        "WHERE status='pending' AND description LIKE '[OC-%' "
        "AND (scheduled_time IS NULL OR scheduled_time <= ?) "
        "ORDER BY priority DESC, scheduled_time ASC LIMIT ?",
        (now, limit)
    ).fetchall()
    conn.close()
    return rows

def update_status(task_id, status):
    conn = sqlite3.connect(str(DB))
    conn.execute("UPDATE openclaw_tasks SET status=?, updated_at=? WHERE id=?",
        (status, datetime.now().isoformat(), task_id))
    conn.commit(); conn.close()

def dispatch(task_id, desc, agent):
    update_status(task_id, 'running')
    # Extraire le contenu sans le préfixe [OC-XX-NN]
    import re
    clean_msg = re.sub(r'^\[OC-[A-Z]+-\d+\]\s*', '', desc)
    cmd = ['openclaw', 'agent', '--agent', agent,
           '--message', clean_msg[:500], '--deliver', '--channel', 'telegram']
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        success = r.returncode == 0
        update_status(task_id, 'completed' if success else 'failed')
        status = '✅' if success else '❌'
        print(f"  {status} [{agent}] {desc[:60]}")
        return success
    except subprocess.TimeoutExpired:
        update_status(task_id, 'failed')
        print(f"  ⏱️  TIMEOUT [{agent}] {desc[:40]}")
        return False
    except Exception as e:
        update_status(task_id, 'failed')
        print(f"  💥 ERR [{agent}] {e}")
        return False

if __name__ == '__main__':
    tasks = get_oc_tasks(limit=int(sys.argv[1]) if len(sys.argv) > 1 else 5)
    print(f'[OC-DISPATCH] {len(tasks)} tâches à dispatcher')
    for tid, desc, agent in tasks:
        dispatch(tid, desc, agent)
    # Stats
    conn = sqlite3.connect(str(DB))
    total = conn.execute("SELECT COUNT(*) FROM openclaw_tasks WHERE description LIKE '[OC-%'").fetchone()[0]
    pending = conn.execute("SELECT COUNT(*) FROM openclaw_tasks WHERE description LIKE '[OC-%' AND status='pending'").fetchone()[0]
    done = conn.execute("SELECT COUNT(*) FROM openclaw_tasks WHERE description LIKE '[OC-%' AND status='completed'").fetchone()[0]
    conn.close()
    print(f'[OC-DISPATCH] Total:{total} | ✅:{done} | ⏳:{pending}')
