#!/usr/bin/env python3
"""Rapport quotidien JARVIS — envoi Telegram à 23h00."""
import os, requests, sqlite3
from pathlib import Path
from datetime import datetime

DB = Path.home() / 'IA/Core/jarvis/data/jarvis-master.db'

def _load_env():
    env = Path.home() / 'Workspaces/jarvis-linux/.env'
    if env.exists():
        for line in env.read_text().splitlines():
            if '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())

def stat(sql):
    try:
        conn = sqlite3.connect(str(DB))
        r = conn.execute(sql).fetchone()
        conn.close()
        return r[0] if r else 0
    except:
        return '?'

def cluster():
    s = {}
    for n, url in [('M1','http://192.168.1.85:1234'),('M2','http://192.168.1.26:1234')]:
        try:
            requests.get(f'{url}/v1/models', timeout=3); s[n] = '✅'
        except:
            s[n] = '❌'
    return s

def send(msg):
    _load_env()
    token = os.environ.get('TELEGRAM_TOKEN') or os.environ.get('TELEGRAM_BOT_TOKEN','')
    chat  = os.environ.get('TELEGRAM_CHAT')  or os.environ.get('TELEGRAM_CHAT_ID','')
    if not token: return False
    r = requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
        json={'chat_id': chat, 'text': msg, 'parse_mode': 'HTML'}, timeout=10)
    return r.ok

if __name__ == '__main__':
    today = datetime.now().strftime('%Y-%m-%d')
    c     = cluster()
    n_codeur = stat(f'SELECT COUNT(*) FROM codeur_projects WHERE scanned_at LIKE "{today}%"')
    msg = f"""📊 <b>JARVIS Daily Report</b> — {today}
═══════════════════════
🖥 Cluster: M1:{c.get('M1')} M2:{c.get('M2')}
💼 Codeur: {n_codeur} projets scannés aujourd'hui
═══════════════════════
Bonne nuit Turbo ! 🌙"""
    print('✅ Rapport envoyé' if send(msg) else '❌ Erreur Telegram')
