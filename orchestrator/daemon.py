#!/usr/bin/env python3
"""
JARVIS Orchestrateur Autonome v1.0
Daemon systemd — Freelance + LinkedIn + Contenu
"""

import asyncio
import aiohttp
import json
import sqlite3
import os
import sys
import signal
import logging
from datetime import datetime, timedelta
from pathlib import Path

# Config
DB_PATH = Path(os.path.expanduser("~/IA/Core/jarvis/orchestrator/jarvis_orchestrator.db"))
CONFIG_PATH = Path(os.path.expanduser("~/.browseros/skills/jarvis-orchestrator/config.yaml"))
LOG_PATH = Path("/tmp/jarvis-orchestrator.log")
CDP_PORT = 9108
LM_STUDIO = "http://127.0.0.1:1234"
CODEUR_USER_ID = "733953"

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("jarvis-orch")

# Keywords filter
KEYWORDS = [
    "ia", "intelligence artificielle", "python", "automatisation", "chatbot",
    "agent", "api", "docker", "linux", "machine learning", "ocr", "data",
    "scraping", "claude", "gpt", "llm", "n8n", "workflow", "voice", "whisper",
    "fastapi", "react", "tensorflow", "pytorch", "nlp", "cuda", "gpu"
]

RUNNING = True

def handle_signal(sig, frame):
    global RUNNING
    log.info(f"Signal {sig} received, shutting down...")
    RUNNING = False

signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)


# ══════════════════════════════════════
# DATABASE
# ══════════════════════════════════════

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS projects_seen (
            id INTEGER PRIMARY KEY,
            codeur_id TEXT UNIQUE,
            title TEXT,
            budget TEXT,
            url TEXT,
            proposals INTEGER,
            matched_keywords TEXT,
            first_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
            offer_sent BOOLEAN DEFAULT 0,
            offer_amount REAL,
            offer_status TEXT DEFAULT 'none'
        );
        CREATE TABLE IF NOT EXISTS action_queue (
            id INTEGER PRIMARY KEY,
            type TEXT NOT NULL,
            priority TEXT DEFAULT 'medium',
            status TEXT DEFAULT 'pending',
            payload TEXT,
            created DATETIME DEFAULT CURRENT_TIMESTAMP,
            executed DATETIME,
            result TEXT,
            channel TEXT
        );
        CREATE TABLE IF NOT EXISTS content_calendar (
            id INTEGER PRIMARY KEY,
            platform TEXT NOT NULL,
            content_type TEXT,
            title TEXT,
            body TEXT,
            image_path TEXT,
            scheduled_at DATETIME,
            published_at DATETIME,
            status TEXT DEFAULT 'draft'
        );
        CREATE TABLE IF NOT EXISTS linkedin_stats (
            id INTEGER PRIMARY KEY,
            date DATE UNIQUE,
            profile_views INTEGER DEFAULT 0,
            post_impressions INTEGER DEFAULT 0,
            comments_received INTEGER DEFAULT 0,
            comments_replied INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS run_log (
            id INTEGER PRIMARY KEY,
            module TEXT,
            started DATETIME,
            finished DATETIME,
            status TEXT,
            details TEXT
        );
    """)
    conn.commit()
    return conn


# ══════════════════════════════════════
# CODEUR.COM WATCHER
# ══════════════════════════════════════

async def scan_codeur_projects(db):
    """Scan Codeur.com for new matching projects."""
    log.info("CODEUR: Scanning projects...")
    start = datetime.now()

    try:
        async with aiohttp.ClientSession() as s:
            matches = []
            for page in range(1, 4):
                url = f"https://www.codeur.com/projects?page={page}"
                try:
                    async with s.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                        if r.status != 200:
                            continue
                        html = await r.text()
                        # Extract project links and titles from HTML
                        import re
                        projects = re.findall(
                            r'href="(/projects/(\d+)-([^"]+))"[^>]*>([^<]+)',
                            html
                        )
                        for href, pid, slug, title in projects:
                            title_lower = title.strip().lower()
                            matched = [k for k in KEYWORDS if k in title_lower or k in slug]
                            if matched:
                                matches.append({
                                    "codeur_id": pid,
                                    "title": title.strip(),
                                    "url": f"https://www.codeur.com{href}",
                                    "matched_keywords": ",".join(matched)
                                })
                except Exception as e:
                    log.warning(f"CODEUR: Page {page} error: {e}")

            # Check for new projects
            c = db.cursor()
            new_matches = []
            for m in matches:
                c.execute("SELECT id FROM projects_seen WHERE codeur_id=?", (m["codeur_id"],))
                if not c.fetchone():
                    c.execute(
                        "INSERT INTO projects_seen (codeur_id, title, url, matched_keywords) VALUES (?,?,?,?)",
                        (m["codeur_id"], m["title"], m["url"], m["matched_keywords"])
                    )
                    new_matches.append(m)
                    log.info(f"CODEUR: NEW MATCH — {m['title']} [{m['matched_keywords']}]")

            db.commit()

            if new_matches:
                # Queue notification for each new match
                for m in new_matches:
                    c.execute(
                        "INSERT INTO action_queue (type, priority, status, payload, channel) VALUES (?,?,?,?,?)",
                        ("new_project", "high", "pending",
                         json.dumps(m, ensure_ascii=False),
                         "telegram")
                    )
                db.commit()
                log.info(f"CODEUR: {len(new_matches)} new matches queued")
            else:
                log.info(f"CODEUR: No new matches (scanned {len(matches)} projects)")

    except Exception as e:
        log.error(f"CODEUR: Scan error: {e}")

    elapsed = (datetime.now() - start).total_seconds()
    db.execute("INSERT INTO run_log (module, started, finished, status, details) VALUES (?,?,?,?,?)",
               ("codeur_scan", start.isoformat(), datetime.now().isoformat(), "ok", f"{len(matches)} scanned"))
    db.commit()


async def check_codeur_messages(db):
    """Check for new client messages via CDP."""
    log.info("CODEUR: Checking messages...")
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://127.0.0.1:{CDP_PORT}/json") as r:
                tabs = await r.json()

            tab = next((t for t in tabs if "codeur" in t.get("url", "") and t.get("type") == "page"), None)
            if not tab:
                log.warning("CODEUR: No Codeur tab in BrowserOS")
                return

            ws = await s.ws_connect(tab["webSocketDebuggerUrl"])
            mid = 0

            async def send(method, params=None):
                nonlocal mid
                mid += 1
                await ws.send_json({"id": mid, "method": method, "params": params or {}})
                for _ in range(50):
                    try:
                        m = await asyncio.wait_for(ws.receive(), timeout=15)
                        if m.type == aiohttp.WSMsgType.TEXT:
                            d = json.loads(m.data)
                            if d.get("id") == mid:
                                return d.get("result", {})
                    except:
                        return {}
                return {}

            await send("Page.enable")
            await send("Runtime.enable")
            await send("Page.navigate", {"url": f"https://www.codeur.com/users/{CODEUR_USER_ID}/messages"})
            await asyncio.sleep(5)

            r = await send("Runtime.evaluate", {"expression": """
                (() => {
                    const unreads = document.querySelectorAll('.unread, [class*=unread], [class*=non-lu]');
                    return unreads.length;
                })()
            """})
            unread = r.get("result", {}).get("value", 0)
            if unread and unread > 0:
                log.info(f"CODEUR: {unread} unread messages!")
                db.execute(
                    "INSERT INTO action_queue (type, priority, status, payload, channel) VALUES (?,?,?,?,?)",
                    ("unread_messages", "urgent", "pending",
                     json.dumps({"count": unread}), "telegram")
                )
                db.commit()

            await ws.close()

    except Exception as e:
        log.warning(f"CODEUR: Message check error: {e}")


# ══════════════════════════════════════
# LINKEDIN WATCHER
# ══════════════════════════════════════

async def check_linkedin(db):
    """Check LinkedIn notifications via CDP."""
    log.info("LINKEDIN: Checking notifications...")
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://127.0.0.1:{CDP_PORT}/json") as r:
                tabs = await r.json()

            tab = next((t for t in tabs if "linkedin" in t.get("url", "") and t.get("type") == "page"), None)
            if not tab:
                log.info("LINKEDIN: No LinkedIn tab — skipping")
                return

            ws = await s.ws_connect(tab["webSocketDebuggerUrl"])
            mid = 0

            async def send(method, params=None):
                nonlocal mid
                mid += 1
                await ws.send_json({"id": mid, "method": method, "params": params or {}})
                for _ in range(50):
                    try:
                        m = await asyncio.wait_for(ws.receive(), timeout=15)
                        if m.type == aiohttp.WSMsgType.TEXT:
                            d = json.loads(m.data)
                            if d.get("id") == mid:
                                return d.get("result", {})
                    except:
                        return {}
                return {}

            await send("Page.enable")
            await send("Runtime.enable")
            await send("Page.navigate", {"url": "https://www.linkedin.com/notifications/"})
            await asyncio.sleep(5)

            r = await send("Runtime.evaluate", {"expression": """
                (() => {
                    const badge = document.querySelector('[class*=notification-badge], .count-badge');
                    return badge ? badge.textContent.trim() : '0';
                })()
            """})
            count = r.get("result", {}).get("value", "0")
            log.info(f"LINKEDIN: {count} notifications")

            # Save daily stats
            today = datetime.now().strftime("%Y-%m-%d")
            db.execute("""
                INSERT OR REPLACE INTO linkedin_stats (date, comments_received)
                VALUES (?, COALESCE((SELECT comments_received FROM linkedin_stats WHERE date=?), 0))
            """, (today, today))
            db.commit()

            await ws.close()

    except Exception as e:
        log.warning(f"LINKEDIN: Check error: {e}")


# ══════════════════════════════════════
# CONTENT GENERATOR
# ══════════════════════════════════════

async def check_content_calendar(db):
    """Check if any content is scheduled for now."""
    log.info("CONTENT: Checking calendar...")
    c = db.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    c.execute(
        "SELECT id, platform, title, body FROM content_calendar WHERE status='scheduled' AND scheduled_at <= ?",
        (now,)
    )
    rows = c.fetchall()
    for row in rows:
        cid, platform, title, body = row
        log.info(f"CONTENT: Ready to publish — {platform}: {title}")
        db.execute(
            "INSERT INTO action_queue (type, priority, status, payload, channel) VALUES (?,?,?,?,?)",
            ("publish_content", "high", "pending",
             json.dumps({"id": cid, "platform": platform, "title": title}, ensure_ascii=False),
             "telegram")
        )
    db.commit()


# ══════════════════════════════════════
# MAIN LOOP
# ══════════════════════════════════════

async def main():
    log.info("=" * 50)
    log.info("JARVIS ORCHESTRATOR v1.0 — STARTING")
    log.info("=" * 50)

    db = init_db()
    log.info(f"Database: {DB_PATH}")

    codeur_interval = 300     # 5 min
    linkedin_interval = 900   # 15 min
    messages_interval = 600   # 10 min
    content_interval = 1800   # 30 min

    last_codeur = 0
    last_linkedin = 0
    last_messages = 0
    last_content = 0

    while RUNNING:
        now = asyncio.get_event_loop().time()

        try:
            # Codeur scan
            if now - last_codeur >= codeur_interval:
                await scan_codeur_projects(db)
                last_codeur = now

            # Codeur messages
            if now - last_messages >= messages_interval:
                await check_codeur_messages(db)
                last_messages = now

            # LinkedIn
            if now - last_linkedin >= linkedin_interval:
                await check_linkedin(db)
                last_linkedin = now

            # Content calendar
            if now - last_content >= content_interval:
                await check_content_calendar(db)
                last_content = now

        except Exception as e:
            log.error(f"LOOP ERROR: {e}")

        await asyncio.sleep(30)

    log.info("JARVIS ORCHESTRATOR — STOPPED")
    db.close()


if __name__ == "__main__":
    asyncio.run(main())
