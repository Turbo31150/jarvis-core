#!/usr/bin/env python3
"""Dispatcher continu OpenClaw — exécute les tâches pending en permanence.
Mode daemon : boucle toutes les 2min, batch de 5 tâches, retry 3x, rapport Telegram 30min.
"""
import sqlite3, subprocess, os, re, time, sys, requests
from pathlib import Path
from datetime import datetime, timedelta

DB      = Path.home() / 'IA/Core/jarvis/data/jarvis-master.db'
LOG     = Path.home() / 'IA/Core/jarvis/logs/oc-dispatcher.log'
SCRIPTS = Path.home() / 'IA/Core/jarvis/scripts'
LOG.parent.mkdir(parents=True, exist_ok=True)

BATCH_SIZE     = 10    # tâches par cycle
CYCLE_SLEEP    = 60    # secondes entre cycles
REPORT_EVERY   = 20    # cycles avant rapport Telegram (~20min)
MAX_RETRIES    = 2     # tentatives par tâche
TASK_TIMEOUT   = 180    # secondes max par tâche

def log(msg: str):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line, flush=True)  # systemd capture vers log file
    # Pas d'écriture fichier directe: systemd StandardOutput=append gère ça

def _load_env():
    env = Path.home() / 'Workspaces/jarvis-linux/.env'
    if env.exists():
        for line in env.read_text().splitlines():
            if '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())

def send_telegram(msg: str):
    _load_env()
    token = os.environ.get('TELEGRAM_TOKEN') or os.environ.get('TELEGRAM_BOT_TOKEN', '')
    chat  = os.environ.get('TELEGRAM_CHAT')  or os.environ.get('TELEGRAM_CHAT_ID', '')
    if not token: return
    try:
        requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
            json={'chat_id': chat, 'text': msg[:4096], 'parse_mode': 'HTML'}, timeout=10)
    except:
        pass

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB))
    conn.execute('PRAGMA busy_timeout=5000')
    return conn

def get_pending_tasks(limit: int) -> list:
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    conn = get_db()
    rows = conn.execute(
        "SELECT id, description, agent, priority, "
        "COALESCE((SELECT COUNT(*) FROM openclaw_tasks t2 WHERE t2.description=openclaw_tasks.description AND t2.status='failed'),0) as retries "
        "FROM openclaw_tasks "
        "WHERE status='pending' "
        "AND (scheduled_time IS NULL OR scheduled_time <= ?) "
        "AND description LIKE '[OC%' "
        "ORDER BY priority DESC, scheduled_time ASC LIMIT ?",
        (now, limit)
    ).fetchall()
    conn.close()
    return [{'id': r[0], 'desc': r[1], 'agent': r[2], 'priority': r[3], 'retries': r[4]} for r in rows]

def update_task(task_id: int, status: str, output: str = ''):
    conn = get_db()
    conn.execute('UPDATE openclaw_tasks SET status=?, updated_at=?, output=? WHERE id=?',
        (status, datetime.now().isoformat(), output[:2000] if output else '', task_id))
    conn.commit(); conn.close()

def requeue_failed_task(task: dict):
    """Recrée une tâche failed comme pending si retries < MAX_RETRIES."""
    if task['retries'] >= MAX_RETRIES:
        log(f"  ⛔ MAX RETRIES atteint pour #{task['id']}: {task['desc'][:40]}")
        return
    conn = get_db()
    conn.execute(
        'INSERT INTO openclaw_tasks (description, agent, status, priority, scheduled_time) VALUES (?,?,?,?,?)',
        (task['desc'], task['agent'], 'pending', task['priority'],
         (datetime.now() + timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M'))
    )
    conn.commit(); conn.close()
    log(f"  🔄 Retry #{task['retries']+1} planifié dans 5min pour: {task['desc'][:40]}")

def dispatch_task(task: dict) -> tuple[bool, str]:
    """Dispatch une tâche vers OpenClaw agent. Retourne (success, output)."""
    # Extraire contenu sans préfixe [OC-XX-NN]
    clean_msg = re.sub(r'^\[OC2?-[A-Z]+-\d+\]\s*', '', task['desc'])
    agent = task.get('agent', 'main') or 'main'

    update_task(task['id'], 'running')
    cmd = ['openclaw', 'agent', '--agent', agent,
           '--message', clean_msg[:2000]]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=TASK_TIMEOUT)
        output = (r.stdout + r.stderr).strip()[:2000]
        success = r.returncode == 0
        update_task(task['id'], 'completed' if success else 'failed', output)
        return success, output
    except subprocess.TimeoutExpired:
        update_task(task['id'], 'failed', 'TIMEOUT')
        return False, 'TIMEOUT'
    except FileNotFoundError:
        update_task(task['id'], 'failed', 'openclaw non trouvé')
        return False, 'openclaw non trouvé'
    except Exception as e:
        update_task(task['id'], 'failed', str(e))
        return False, str(e)

def get_stats() -> dict:
    conn = get_db()
    stats = {}
    for status in ('pending', 'running', 'completed', 'failed'):
        stats[status] = conn.execute(
            "SELECT COUNT(*) FROM openclaw_tasks WHERE description LIKE '[OC%' AND status=?",
            (status,)
        ).fetchone()[0]
    stats['total'] = sum(stats.values())
    conn.close()
    return stats

def run_cycle() -> tuple[int, int]:
    """Exécute un cycle de dispatch. Retourne (done, failed)."""
    tasks = get_pending_tasks(BATCH_SIZE)
    if not tasks:
        return 0, 0

    done = failed = 0
    for task in tasks:
        log(f"  → #{task['id']} [{task['agent']}] {task['desc'][:55]}")
        success, output = dispatch_task(task)
        if success:
            done += 1
            log(f"    ✅ OK")
        else:
            failed += 1
            log(f"    ❌ FAIL: {output[:80]}")
            requeue_failed_task(task)
        time.sleep(1)  # Petit délai entre tâches

    return done, failed


def cleanup_stale_running(max_minutes: int = 5):
    """Remet en pending les tâches running depuis trop longtemps."""
    from datetime import datetime, timedelta
    conn = get_db()
    rows = conn.execute("SELECT id, updated_at FROM openclaw_tasks WHERE status='running'").fetchall()
    now = datetime.now()
    reset_ids = []
    for row_id, updated_at in rows:
        try:
            dt = datetime.fromisoformat(str(updated_at).replace('Z',''))
            if (now - dt).total_seconds() / 60 > max_minutes:
                reset_ids.append(row_id)
        except:
            reset_ids.append(row_id)
    if reset_ids:
        conn.execute(f"UPDATE openclaw_tasks SET status='pending', output=NULL WHERE id IN ({','.join(map(str,reset_ids))})")
        conn.commit()
        log(f"[CLEANUP] {len(reset_ids)} tâches running bloquées → pending")
    conn.close()

def main():
    log('[OC-DISPATCHER] Démarrage daemon — batch=5, cycle=2min')
    send_telegram('🤖 <b>OC-Dispatcher démarré</b> — dispatche les tâches OpenClaw automatiquement')

    cycle_count = 0
    total_done = total_failed = 0

    while True:
        try:
            cleanup_stale_running()
            stats = get_stats()
            if stats['pending'] > 0:
                log(f'[CYCLE {cycle_count+1}] {stats["pending"]} tâches pending')
                done, failed = run_cycle()
                total_done += done; total_failed += failed
                log(f'[CYCLE {cycle_count+1}] ✅{done} ❌{failed} | Total: ✅{total_done} ❌{total_failed}')
            else:
                log(f'[CYCLE {cycle_count+1}] Aucune tâche pending ({stats["completed"]} completed, {stats["failed"]} failed)')

            cycle_count += 1

            # Rapport Telegram toutes les REPORT_EVERY cycles
            if cycle_count % REPORT_EVERY == 0:
                stats = get_stats()
                pct = round(stats['completed'] / max(stats['total'], 1) * 100)
                send_telegram(
                    f'📊 <b>OC-Dispatcher — Rapport</b> {datetime.now().strftime("%H:%M")}\n'
                    f'⏳ Pending: {stats["pending"]}\n'
                    f'✅ Completed: {stats["completed"]} ({pct}%)\n'
                    f'❌ Failed: {stats["failed"]}\n'
                    f'🔄 Total: {stats["total"]}'
                )

        except KeyboardInterrupt:
            log('[OC-DISPATCHER] Arrêt sur Ctrl+C')
            send_telegram('⚠️ OC-Dispatcher arrêté manuellement')
            sys.exit(0)
        except Exception as e:
            log(f'[OC-DISPATCHER] ERREUR CYCLE: {e}')
            time.sleep(30)  # Pause courte sur erreur, pas de crash

        time.sleep(CYCLE_SLEEP)

if __name__ == '__main__':
    if '--once' in sys.argv:
        # Mode single-run pour tests
        done, failed = run_cycle()
        stats = get_stats()
        print(f'Done: {done}, Failed: {failed} | Stats: {stats}')
    else:
        main()
