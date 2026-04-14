#!/usr/bin/env python3
"""Génère 1 post LinkedIn par jour avec M2/qwen3.5-35b. Envoie via Telegram pour validation."""
import os, requests, json, sys
from pathlib import Path
from datetime import datetime

TOPICS_FILE = Path.home() / '.openclaw/content/topics.json'
POSTS_DIR   = Path.home() / '.openclaw/content/posts'
POSTS_DIR.mkdir(parents=True, exist_ok=True)

def _load_env():
    env = Path.home() / 'Workspaces/jarvis-linux/.env'
    if env.exists():
        for line in env.read_text().splitlines():
            if '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())

def get_topic() -> str:
    topics = json.loads(TOPICS_FILE.read_text())['linkedin_topics']
    day_idx = datetime.now().timetuple().tm_yday % len(topics)
    return topics[day_idx]

def generate_post(topic: str) -> str:
    prompt = f"""Tu es Franc Delmas (Turbo), développeur IA freelance français, expert en LLMs locaux et automatisation.
Écris un post LinkedIn viral sur ce sujet : {topic}

Format obligatoire:
- 1 ligne d'accroche percutante (commence par une stat, question, ou assertion forte)
- Saut de ligne
- 5-7 points concrets avec emojis (expérience personnelle, chiffres réels)
- Saut de ligne
- 1 question pour engager la communauté
- Saut de ligne
- 5-7 hashtags pertinents (#IA #Python #LLM #Freelance #Dev etc.)

Ton: authentique, technique mais accessible, première personne. Max 1300 caractères."""

    try:
        r = requests.post('http://192.168.1.26:1234/v1/chat/completions',
            json={'model': 'qwen3.5-35b-a3b',
                  'messages': [{'role': 'user', 'content': prompt}],
                  'max_tokens': 600},
            timeout=60)
        if r.ok:
            return r.json()['choices'][0]['message']['content']
    except:
        pass
    # Fallback M1
    r = requests.post('http://192.168.1.85:1234/v1/chat/completions',
        json={'model': 'qwen3.5-9b',
              'messages': [{'role': 'user', 'content': prompt}],
              'max_tokens': 600},
        timeout=30)
    return r.json()['choices'][0]['message']['content']

def send_for_validation(topic: str, post: str):
    _load_env()
    token = os.environ.get('TELEGRAM_TOKEN') or os.environ.get('TELEGRAM_BOT_TOKEN', '')
    chat  = os.environ.get('TELEGRAM_CHAT')  or os.environ.get('TELEGRAM_CHAT_ID', '')
    if not token:
        print('[TELEGRAM] Token manquant'); return
    msg = f"""✍️ <b>Post LinkedIn du jour</b> — {datetime.now().strftime('%d/%m/%Y')}
<b>Sujet:</b> {topic}

━━━━━━━━━━━━━━━━
{post}
━━━━━━━━━━━━━━━━
<i>Répondre OK pour publier ou SKIP pour ignorer</i>"""
    requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
        json={'chat_id': chat, 'text': msg, 'parse_mode': 'HTML'}, timeout=10)

if __name__ == '__main__':
    topic = get_topic()
    print(f'[CONTENT] Sujet: {topic}')
    post = generate_post(topic)
    today = datetime.now().strftime('%Y-%m-%d')
    out = POSTS_DIR / f'{today}.md'
    out.write_text(f'# {topic}\n\n{post}\n')
    print(f'[CONTENT] Sauvegardé: {out}')
    send_for_validation(topic, post)
    print('[CONTENT] Envoyé Telegram pour validation')
