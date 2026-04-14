#!/usr/bin/env python3
"""Scrape DevPost pour les hackathons actifs pertinents. Alerte Telegram."""
import os, requests, json
from pathlib import Path
from datetime import datetime
from bs4 import BeautifulSoup

KEYWORDS = ['ai','machine learning','python','automation','llm','open source','api','data','iot','web3']
MIN_PRIZE = 0  # 0 = tous les hackathons (y compris sans prize)

def _load_env():
    env = Path.home() / 'Workspaces/jarvis-linux/.env'
    if env.exists():
        for line in env.read_text().splitlines():
            if '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())

def scrape_devpost() -> list:
    import re
    url = 'https://devpost.com/api/hackathons?status=open&order_by=prize-amount&page=1'
    headers = {'Accept': 'application/json', 'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'}
    try:
        r = requests.get(url, headers=headers, timeout=20)
        hacks = r.json().get('hackathons', [])
        hackathons = []
        for h in hacks[:30]:
            title = h.get('title', 'N/A')
            prize_raw = str(h.get('prize_amount', 'No prize'))
            # Strip HTML tags from prize
            prize = re.sub(r'<[^>]+>', '', prize_raw).strip() or 'No prize'
            deadline = h.get('submission_period_dates', 'N/A')
            link = h.get('url', '')
            themes = ' '.join([t.get('name', '') for t in h.get('themes', [])])
            text = (title + ' ' + themes).lower()
            if any(kw in text for kw in KEYWORDS):
                hackathons.append({'title': title, 'prize': prize, 'deadline': deadline, 'url': link})
        return hackathons
    except Exception as e:
        return [{'error': str(e)}]

def send_telegram(hackathons: list):
    _load_env()
    token = os.environ.get('TELEGRAM_TOKEN') or os.environ.get('TELEGRAM_BOT_TOKEN', '')
    chat  = os.environ.get('TELEGRAM_CHAT')  or os.environ.get('TELEGRAM_CHAT_ID', '')
    if not token or not hackathons:
        return
    lines = [f'🏆 <b>Hackathons DevPost actifs</b> — {datetime.now().strftime("%d/%m/%Y")}\n']
    for h in hackathons[:8]:
        if 'error' in h:
            lines.append(f'❌ Erreur: {h["error"]}')
        else:
            lines.append(f'• <b>{h["title"]}</b>\n  💰 {h["prize"]} | ⏰ {h["deadline"]}\n  🔗 {h["url"]}')
    requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
        json={'chat_id': chat, 'text': '\n'.join(lines), 'parse_mode': 'HTML',
              'disable_web_page_preview': True}, timeout=10)

if __name__ == '__main__':
    print('[DEVPOST] Scraping hackathons...')
    hackathons = scrape_devpost()
    print(f'[DEVPOST] {len(hackathons)} hackathons trouvés')
    for h in hackathons:
        print(f"  • {h.get('title','?')} — {h.get('prize','?')}")
    send_telegram(hackathons)
    # Sauvegarder
    out = Path.home() / 'IA/Core/jarvis/data/devpost-hackathons.json'
    out.write_text(json.dumps(hackathons, indent=2, ensure_ascii=False))
    print(f'[DEVPOST] Sauvegardé: {out}')
