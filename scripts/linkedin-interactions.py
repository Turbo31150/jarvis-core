#!/usr/bin/env python3
"""Gestion interactions LinkedIn : commentaires, mentions, réponses — via Playwright CDP."""
import subprocess, requests, os, json
from pathlib import Path
from datetime import datetime

def _load_env():
    env = Path.home() / 'Workspaces/jarvis-linux/.env'
    if env.exists():
        for line in env.read_text().splitlines():
            if '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())

def get_chrome_tabs():
    try:
        r = requests.get('http://127.0.0.1:9222/json', timeout=3)
        return r.json()
    except:
        return []

def generate_reply(comment: str, context: str) -> str:
    """Génère une réponse à un commentaire LinkedIn avec M1/qwen3.5-9b."""
    prompt = f"""Tu es Franc Delmas, développeur IA freelance. Réponds à ce commentaire LinkedIn de façon authentique, en 1-3 phrases. Professionnel mais chaleureux. En français.

Post original: {context[:200]}
Commentaire: {comment}

Réponse (max 150 caractères):"""
    try:
        r = requests.post('http://192.168.1.85:1234/v1/chat/completions',
            json={'model': 'qwen3.5-9b',
                  'messages': [{'role': 'user', 'content': prompt}],
                  'max_tokens': 100},
            timeout=15)
        return r.json()['choices'][0]['message']['content'].strip()
    except:
        return "Merci pour votre commentaire ! 🙏"

def check_linkedin_notifications():
    """Vérifie les notifications LinkedIn via Chrome CDP."""
    tabs = get_chrome_tabs()
    linkedin_tabs = [t for t in tabs if 'linkedin.com' in t.get('url', '')]
    if not linkedin_tabs:
        return {'status': 'no_linkedin_tab', 'notifications': 0}

    tab_id = linkedin_tabs[0]['id']
    # Récupérer le badge de notifications
    script = """
    const badge = document.querySelector('.notification-badge') ||
                  document.querySelector('[data-control-name="nav.notifications"]');
    badge ? badge.textContent.trim() : '0'
    """
    try:
        r = requests.post(f'http://127.0.0.1:9222/json/runtime/evaluate/{tab_id}',
            json={'expression': script}, timeout=5)
        count = r.json().get('result', {}).get('value', '0')
        return {'status': 'ok', 'notifications': count, 'tab_id': tab_id}
    except:
        return {'status': 'error', 'notifications': '?'}

def send_report(data: dict):
    _load_env()
    token = os.environ.get('TELEGRAM_TOKEN') or os.environ.get('TELEGRAM_BOT_TOKEN', '')
    chat  = os.environ.get('TELEGRAM_CHAT')  or os.environ.get('TELEGRAM_CHAT_ID', '')
    if not token: return
    msg = f"""📱 <b>LinkedIn Interactions</b> — {datetime.now().strftime('%H:%M')}
Status: {data.get('status','?')}
Notifications: {data.get('notifications','?')}
<i>Ouvrir LinkedIn pour traiter manuellement les interactions</i>"""
    requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
        json={'chat_id': chat, 'text': msg, 'parse_mode': 'HTML'}, timeout=10)

if __name__ == '__main__':
    print('[LINKEDIN] Vérification interactions...')
    data = check_linkedin_notifications()
    print(f'[LINKEDIN] {data}')
    send_report(data)
