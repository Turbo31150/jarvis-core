#!/usr/bin/env python3
"""Vérificateur résultats tâches OpenClaw — détecte les faux succès (output vide/générique).
Requeue les tâches dont le résultat est insuffisant.
"""
import sqlite3, re
from pathlib import Path
from datetime import datetime, timedelta

DB  = Path.home() / 'IA/Core/jarvis/data/jarvis-master.db'
LOG = Path.home() / 'IA/Core/jarvis/logs/oc-verifier.log'
LOG.parent.mkdir(parents=True, exist_ok=True)

# Patterns indiquant une réponse vide/générique insuffisante
EMPTY_PATTERNS = [
    r'^OK$', r'^ok$', r'^\s*$', r'^None$',
    r'^Je vais', r'^Je veux', r'^D\'accord',
    r'cannot.*assist', r'I cannot', r'As an AI',
    r'^.{0,20}$',  # Réponse trop courte (<20 chars)
]

def log(msg: str):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line)
    with open(LOG, 'a') as f:
        f.write(line + '\n')

def is_valid_output(output: str) -> bool:
    """Vérifie que l'output est substantiel et non générique."""
    if not output or not output.strip():
        return False
    for pattern in EMPTY_PATTERNS:
        if re.search(pattern, output.strip(), re.IGNORECASE):
            return False
    return True

def verify_and_requeue():
    conn = sqlite3.connect(str(DB))
    conn.execute('PRAGMA busy_timeout=5000')

    # Tâches completed avec output suspect
    rows = conn.execute(
        "SELECT id, description, agent, priority, output FROM openclaw_tasks "
        "WHERE status='completed' AND description LIKE '[OC%' "
        "AND updated_at > datetime('now', '-24 hours') "
        "ORDER BY updated_at DESC LIMIT 100"
    ).fetchall()

    requeued = verified = 0
    for task_id, desc, agent, priority, output in rows:
        if is_valid_output(output or ''):
            verified += 1
        else:
            # Marquer comme failed + requeue
            conn.execute("UPDATE openclaw_tasks SET status='failed', output='INVALID_OUTPUT' WHERE id=?", (task_id,))
            # Requeue si pas trop de retries
            retry_count = conn.execute(
                "SELECT COUNT(*) FROM openclaw_tasks WHERE description=? AND status='failed'", (desc,)
            ).fetchone()[0]
            if retry_count < 3:
                conn.execute(
                    'INSERT INTO openclaw_tasks (description, agent, status, priority, scheduled_time) VALUES (?,?,?,?,?)',
                    (desc, agent, 'pending', priority,
                     (datetime.now() + timedelta(minutes=10)).strftime('%Y-%m-%d %H:%M'))
                )
                requeued += 1
                log(f'  🔄 Requeue: {desc[:50]}')
            else:
                log(f'  ⛔ Drop (3 retries): {desc[:50]}')

    conn.commit()

    # Stats globales
    stats = {}
    for status in ('pending', 'running', 'completed', 'failed'):
        stats[status] = conn.execute(
            "SELECT COUNT(*) FROM openclaw_tasks WHERE description LIKE '[OC%' AND status=?", (status,)
        ).fetchone()[0]
    conn.close()

    log(f'Vérification: {verified} OK, {requeued} requeueés')
    log(f'Stats: pending={stats["pending"]} completed={stats["completed"]} failed={stats["failed"]}')

    pct = round(stats['completed'] / max(sum(stats.values()), 1) * 100)
    print(f'\n📊 Progression OpenClaw: {pct}% ({stats["completed"]}/{sum(stats.values())})')
    return stats

if __name__ == '__main__':
    log('[OC-VERIFIER] Démarrage vérification')
    verify_and_requeue()
