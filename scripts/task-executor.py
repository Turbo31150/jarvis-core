#!/usr/bin/env python3
"""Exécuteur de tâches planifiées — lit openclaw_tasks et exécute les scripts."""
import sqlite3, subprocess, os, sys, requests
from pathlib import Path
from datetime import datetime

DB      = Path.home() / 'IA/Core/jarvis/data/jarvis-master.db'
SCRIPTS = Path.home() / 'IA/Core/jarvis/scripts'

# Map description → script à exécuter
TASK_MAP = {
    'post linkedin': f'python3 {SCRIPTS}/content-machine.py',
    'daily briefing': f'python3 {SCRIPTS}/daily-briefing.py',
    'scan codeur': f'python3 {SCRIPTS}/codeur-veille.py --once',
    'devpost': f'python3 {SCRIPTS}/devpost-scraper.py',
    'candidatures codeur': f'python3 {SCRIPTS}/codeur-auto-apply.py',
    'linkedin interactions': f'python3 {SCRIPTS}/linkedin-interactions.py',
    'daily report': f'python3 {SCRIPTS}/daily-report.py',
    'self-improve': f'python3 {SCRIPTS}/self-improve.py',
    'backup': f'bash {SCRIPTS}/backup-openclaw.sh',
}

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
    requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
        json={'chat_id': chat, 'text': msg, 'parse_mode': 'HTML'}, timeout=10)

def get_pending_tasks(limit: int = 5) -> list:
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    conn = sqlite3.connect(str(DB))
    rows = conn.execute(
        'SELECT id, description, agent, scheduled_time FROM openclaw_tasks '
        'WHERE status=? AND (scheduled_time IS NULL OR scheduled_time <= ?) '
        'ORDER BY priority DESC, scheduled_time ASC LIMIT ?',
        ('pending', now, limit)
    ).fetchall()
    conn.close()
    return [{'id': r[0], 'desc': r[1], 'agent': r[2], 'time': r[3]} for r in rows]

def update_task(task_id: int, status: str, output: str = ''):
    conn = sqlite3.connect(str(DB))
    conn.execute('UPDATE openclaw_tasks SET status=?, updated_at=? WHERE id=?',
        (status, datetime.now().isoformat(), task_id))
    conn.commit(); conn.close()

def find_script(desc: str) -> str | None:
    desc_lower = desc.lower()
    for keyword, script in TASK_MAP.items():
        if keyword in desc_lower:
            return script
    return None

def execute_task(task: dict) -> bool:
    script = find_script(task['desc'])
    print(f"[EXECUTOR] #{task['id']} {task['desc'][:50]}")
    if script:
        update_task(task['id'], 'running')
        r = subprocess.run(script, shell=True, capture_output=True, text=True, timeout=120)
        success = r.returncode == 0
        update_task(task['id'], 'completed' if success else 'failed')
        status = '✅' if success else '❌'
        print(f"  {status} exit={r.returncode}")
        return success
    else:
        # Dispatcher vers OpenClaw agent
        agent = task.get('agent', 'main')
        update_task(task['id'], 'running')
        cmd = ['openclaw', 'agent', '--agent', agent,
               '--message', task['desc'], '--deliver', '--channel', 'telegram']
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        update_task(task['id'], 'completed')
        print(f"  → Dispatché vers {agent}")
        return True

def run_all_pending():
    tasks = get_pending_tasks(limit=10)
    if not tasks:
        print('[EXECUTOR] Aucune tâche en attente')
        return
    print(f'[EXECUTOR] {len(tasks)} tâches à exécuter')
    done, failed = 0, 0
    for task in tasks:
        try:
            ok = execute_task(task)
            if ok: done += 1
            else:  failed += 1
        except Exception as e:
            print(f'  ❌ Erreur: {e}')
            update_task(task['id'], 'failed')
            failed += 1
    send_telegram(f'⚙️ <b>Task Executor</b> — {datetime.now().strftime("%H:%M")}\n✅ {done} OK | ❌ {failed} erreurs')

if __name__ == '__main__':
    if '--loop' in sys.argv:
        import time
        print('[EXECUTOR] Mode daemon — vérification toutes les 5min')
        while True:
            run_all_pending()
            time.sleep(300)
    else:
        run_all_pending()
