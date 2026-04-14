#!/usr/bin/env python3
"""Daily briefing JARVIS — envoi Telegram à 8h00."""
import sys, os
sys.path.insert(0, str(__import__('pathlib').Path.home() / '.openclaw/tools'))

import requests, sqlite3, subprocess
from pathlib import Path
from datetime import datetime, timedelta

DB = Path.home() / 'IA/Core/jarvis/data/jarvis-master.db'

def _load_env():
    env = Path.home() / 'Workspaces/jarvis-linux/.env'
    if env.exists():
        for line in env.read_text().splitlines():
            if '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())

def cluster_status():
    r = {}
    for n, url in [('M1','http://192.168.1.85:1234'),('M2','http://192.168.1.26:1234')]:
        try:
            requests.get(f'{url}/v1/models', timeout=3)
            r[n] = '✅'
        except:
            r[n] = '❌'
    return r

def count_codeur():
    try:
        conn = sqlite3.connect(str(DB))
        yesterday = (datetime.now() - timedelta(days=1)).isoformat()[:10]
        n = conn.execute(
            'SELECT COUNT(*) FROM codeur_projects WHERE scanned_at >= ?', (yesterday,)
        ).fetchone()[0]
        conn.close()
        return n
    except:
        return '?'

def gpu_temps():
    try:
        out = subprocess.check_output(
            ['nvidia-smi','--query-gpu=temperature.gpu','--format=csv,noheader'], text=True)
        temps = [int(t.strip()) for t in out.strip().split('\n') if t.strip().isdigit()]
        return f'{max(temps)}°C max ({len(temps)} GPUs)'
    except:
        return 'N/A'

def send_telegram(msg):
    _load_env()
    token = os.environ.get('TELEGRAM_TOKEN') or os.environ.get('TELEGRAM_BOT_TOKEN','')
    chat  = os.environ.get('TELEGRAM_CHAT')  or os.environ.get('TELEGRAM_CHAT_ID','')
    if not token:
        print('Token manquant'); return False
    r = requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
        json={'chat_id': chat, 'text': msg, 'parse_mode': 'HTML',
              'disable_web_page_preview': True}, timeout=10)
    return r.ok

if __name__ == '__main__':
    c = cluster_status()
    msg = f"""🌅 <b>JARVIS Daily Briefing</b> — {datetime.now().strftime('%d/%m/%Y %H:%M')}
═══════���═══════════════
🖥 Cluster: M1:{c.get('M1','?')} M2:{c.get('M2','?')}
💼 Codeur.com: {count_codeur()} projets (J-1)
🔥 GPUs: {gpu_temps()}
═══════════════════════
Bonne journée Turbo ! 🚀"""
    ok = send_telegram(msg)
    print('✅ Briefing envoyé' if ok else '❌ Erreur Telegram')
