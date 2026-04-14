#!/usr/bin/env python3
"""Auto-amélioration JARVIS : analyse les logs → identifie les patterns → propose des fixes."""
import os, requests, json, sqlite3
from pathlib import Path
from datetime import datetime, timedelta

LOGS_DIR = Path.home() / '.openclaw/logs'
DB       = Path.home() / 'IA/Core/jarvis/data/jarvis-master.db'

def _load_env():
    env = Path.home() / 'Workspaces/jarvis-linux/.env'
    if env.exists():
        for line in env.read_text().splitlines():
            if '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())

def collect_errors() -> str:
    """Collecte les erreurs des 7 derniers jours."""
    errors = []
    cutoff = datetime.now() - timedelta(days=7)
    # Logs OpenClaw
    for log in LOGS_DIR.glob('*.log'):
        try:
            for line in log.read_text().splitlines():
                if 'ERROR' in line or 'FAILED' in line or 'Exception' in line:
                    errors.append(line.strip())
        except:
            pass
    # Logs JARVIS
    jarvis_log = Path.home() / 'IA/Core/jarvis/logs/codeur-veille.log'
    if jarvis_log.exists():
        for line in jarvis_log.read_text().splitlines()[-200:]:
            if 'ERROR' in line or 'WARNING' in line:
                errors.append(line.strip())
    return '\n'.join(errors[-100:])  # Max 100 erreurs

def analyze_with_llm(errors: str) -> str:
    prompt = f"""Tu es un expert DevOps/Python. Analyse ces logs d'erreurs JARVIS des 7 derniers jours.
Identifie les 5 problèmes les plus fréquents et propose une correction concrète pour chacun.
Format:
1. [PROBLÈME] description → [FIX] solution Python/bash

Logs:
{errors[:3000]}"""
    for url, model in [
        ('http://192.168.1.26:1234', 'deepseek-r1-0528'),
        ('http://192.168.1.85:1234', 'qwen3.5-9b'),
    ]:
        try:
            r = requests.post(f'{url}/v1/chat/completions',
                json={'model': model, 'messages': [{'role':'user','content':prompt}], 'max_tokens':800},
                timeout=60)
            if r.ok:
                return r.json()['choices'][0]['message']['content']
        except:
            pass
    return 'Analyse impossible — cluster indisponible'

def send_report(analysis: str):
    _load_env()
    token = os.environ.get('TELEGRAM_TOKEN') or os.environ.get('TELEGRAM_BOT_TOKEN', '')
    chat  = os.environ.get('TELEGRAM_CHAT')  or os.environ.get('TELEGRAM_CHAT_ID', '')
    if not token: return
    msg = f"""🔧 <b>JARVIS Self-Improve Report</b> — {datetime.now().strftime('%d/%m/%Y')}

{analysis[:3000]}"""
    requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
        json={'chat_id': chat, 'text': msg, 'parse_mode': 'HTML'}, timeout=15)

if __name__ == '__main__':
    print('[SELF-IMPROVE] Collecte des erreurs...')
    errors = collect_errors()
    print(f'[SELF-IMPROVE] {len(errors.splitlines())} erreurs collectées')
    if errors.strip():
        analysis = analyze_with_llm(errors)
        print(f'[SELF-IMPROVE] Analyse:\n{analysis[:500]}...')
        send_report(analysis)
    else:
        print('[SELF-IMPROVE] Aucune erreur récente — système sain ✅')
