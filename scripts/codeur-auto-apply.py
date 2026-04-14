#!/usr/bin/env python3
"""Génère des candidatures Codeur.com pour les projets matchés. Validation Telegram avant envoi."""
import os, requests, sqlite3, json
from pathlib import Path
from datetime import datetime

DB      = Path.home() / 'IA/Core/jarvis/data/jarvis-master.db'
OUT_DIR = Path.home() / '.openclaw/content/candidatures'
OUT_DIR.mkdir(parents=True, exist_ok=True)

def _load_env():
    env = Path.home() / 'Workspaces/jarvis-linux/.env'
    if env.exists():
        for line in env.read_text().splitlines():
            if '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())

def get_pending_projects() -> list:
    try:
        conn = sqlite3.connect(str(DB))
        rows = conn.execute(
            'SELECT pid, title, budget, keywords FROM codeur_projects '
            'WHERE (applied IS NULL OR applied=0) AND score >= 30 '
            'ORDER BY score DESC LIMIT 5'
        ).fetchall()
        conn.close()
        return [{'pid': r[0], 'title': r[1], 'budget': r[2], 'keywords': r[3]} for r in rows]
    except Exception as e:
        print(f'[APPLY] DB error: {e}')
        return []

def generate_application(project: dict) -> str:
    prompt = f"""Tu es Franc Delmas, développeur IA freelance expert Python/automatisation/LLM.
Écris une candidature Codeur.com percutante pour ce projet:
Titre: {project['title']}
Budget: {project['budget']}
Mots-clés: {project['keywords']}

Format:
- 1 phrase d'accroche personnalisée au projet
- 2-3 phrases sur ton expertise pertinente (avec exemples concrets)
- 1 phrase sur ta disponibilité et TJM indicatif (350-500€)
- Ton professionnel et direct. Max 200 mots."""
    try:
        r = requests.post('http://192.168.1.85:1234/v1/chat/completions',
            json={'model': 'qwen3.5-9b',
                  'messages': [{'role': 'user', 'content': prompt}],
                  'max_tokens': 300},
            timeout=20)
        return r.json()['choices'][0]['message']['content'].strip()
    except Exception as e:
        return f'Erreur génération: {e}'

def send_for_validation(project: dict, application: str):
    _load_env()
    token = os.environ.get('TELEGRAM_TOKEN') or os.environ.get('TELEGRAM_BOT_TOKEN', '')
    chat  = os.environ.get('TELEGRAM_CHAT')  or os.environ.get('TELEGRAM_CHAT_ID', '')
    if not token: return
    url = f"https://www.codeur.com/projects/{project['pid']}"
    msg = f"""💼 <b>Candidature Codeur.com</b> — À VALIDER

<b>{project['title']}</b>
💰 {project['budget']}
🔗 {url}

━━━━━━━━━━
{application}
━━━━━━━━━━
<i>⚠️ Validation manuelle requise — ouvrir le lien et copier le texte</i>"""
    requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
        json={'chat_id': chat, 'text': msg, 'parse_mode': 'HTML',
              'disable_web_page_preview': False}, timeout=10)

if __name__ == '__main__':
    projects = get_pending_projects()
    print(f'[APPLY] {len(projects)} projets à traiter')
    for p in projects:
        print(f"  → {p['title']} ({p['budget']})")
        app_text = generate_application(p)
        # Sauvegarder
        out = OUT_DIR / f"{p['pid']}-{datetime.now().strftime('%Y%m%d')}.md"
        out.write_text(f"# {p['title']}\n\n{app_text}\n\nURL: https://www.codeur.com/projects/{p['pid']}\n")
        send_for_validation(p, app_text)
        print(f'  ✅ Candidature générée et envoyée Telegram')
