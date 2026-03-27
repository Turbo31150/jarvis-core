#!/usr/bin/env python3
"""JARVIS Telegram Alerts — Send notifications to Telegram."""
import os, json, requests, sqlite3, time
from pathlib import Path

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
DB = Path("/home/turbo/IA/Core/jarvis/data/jarvis-master.db")

def send(message, parse_mode="Markdown"):
    """Send a Telegram message."""
    if not BOT_TOKEN or not CHAT_ID:
        print(f"[TELEGRAM] No token/chat_id — would send: {message[:80]}")
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": message, "parse_mode": parse_mode}, timeout=10)
        return r.json().get("ok", False)
    except:
        return False

def alert_new_project(pid, title, budget, keywords):
    """Alert when a new matching project is found."""
    msg = f"🔔 *Nouveau projet Codeur*\n📋 {title}\n💰 {budget}\n🔑 {', '.join(keywords)}\n🔗 https://www.codeur.com/projects/{pid}"
    return send(msg)

def alert_client_message(client, project):
    """Alert when a client responds."""
    msg = f"💬 *Message client!*\n👤 {client}\n📋 {project}\n⚡ Répondre rapidement!"
    return send(msg)

def alert_linkedin(action, person, content=""):
    """Alert for LinkedIn engagement."""
    msg = f"📱 *LinkedIn*\n{action}: {person}\n{content[:100]}"
    return send(msg)

def daily_digest():
    """Send daily stats digest."""
    conn = sqlite3.connect(str(DB))
    c = conn.cursor()
    offers = c.execute("SELECT COUNT(*), SUM(amount) FROM codeur_offers").fetchone()
    runs = c.execute("SELECT COUNT(*) FROM workflow_runs").fetchone()[0]
    actions = c.execute("SELECT COUNT(*) FROM linkedin_actions").fetchone()[0]
    conn.close()
    
    msg = f"""📊 *JARVIS Daily Digest*
🏢 Offres: {offers[0]} ({offers[1]}€)
🔄 Workflow runs: {runs}
📱 LinkedIn actions: {actions}
⏰ {time.strftime('%Y-%m-%d %H:%M')}"""
    return send(msg)

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        if sys.argv[1] == "digest": daily_digest()
        elif sys.argv[1] == "test": send("🛰️ JARVIS Telegram: test OK!")
        else: send(" ".join(sys.argv[1:]))
    else:
        daily_digest()
