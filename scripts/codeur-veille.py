#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
codeur-veille.py — Monitoring Codeur.com for AI/automation projects with Telegram alerts.

Usage:
    # Daemon mode (checks every 30 minutes by default):
    python3 codeur-veille.py

    # Single check (for cron):
    python3 codeur-veille.py --once

    # Custom interval (in minutes):
    python3 codeur-veille.py --interval 15

    # Dry run (no Telegram, just print):
    python3 codeur-veille.py --once --dry-run

Environment variables required:
    TELEGRAM_BOT_TOKEN  — Telegram bot token from @BotFather
    TELEGRAM_CHAT_ID    — Telegram chat ID to send alerts to

Cron example (every 30 min):
    */30 * * * * cd /home/turbo/IA/Core/jarvis && python3 scripts/codeur-veille.py --once

Files:
    Seen projects DB : /home/turbo/IA/Core/jarvis/data/codeur-seen-projects.json
    Log file         : /home/turbo/IA/Core/jarvis/logs/codeur-veille.log
"""

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path("/home/turbo/IA/Core/jarvis")
SEEN_PROJECTS_FILE = BASE_DIR / "data" / "codeur-seen-projects.json"
DB_PATH = BASE_DIR / "data" / "jarvis-master.db"
LOG_FILE = BASE_DIR / "logs" / "codeur-veille.log"

# ---------------------------------------------------------------------------
# Search config
# ---------------------------------------------------------------------------
CODEUR_BASE_URL = "https://www.codeur.com"
PAGES_TO_SCRAPE = 3  # Number of listing pages to check (35 projects/page)

MATCH_KEYWORDS = [
    "ia", "intelligence artificielle", "machine learning", "chatbot",
    "automatisation", "python", "api", "scraping", "trading", "agent",
    "voice", "vocal", "whisper", "gpu", "docker", "n8n", "workflow",
]

MIN_BUDGET_EUR = 500

DEFAULT_INTERVAL_MINUTES = 30
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 10

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("codeur-veille")
logger.setLevel(logging.DEBUG)

fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
fh.setLevel(logging.DEBUG)
fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(fh)

ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.INFO)
ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(ch)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_seen_projects() -> dict:
    """Load the set of already-seen project IDs."""
    if SEEN_PROJECTS_FILE.exists():
        try:
            with open(SEEN_PROJECTS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning("Could not load seen projects file, starting fresh: %s", e)
    return {}


def save_seen_projects(seen: dict) -> None:
    """Persist seen project IDs to JSON."""
    SEEN_PROJECTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SEEN_PROJECTS_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)


def fetch_page(url: str) -> str | None:
    """Fetch a page with retries."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "fr-FR,fr;q=0.9",
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            logger.warning("Fetch attempt %d/%d failed for %s: %s", attempt, MAX_RETRIES, url, e)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)
    logger.error("All %d fetch attempts failed for %s", MAX_RETRIES, url)
    return None


def parse_budget(text: str) -> int:
    """Extract the minimum budget value in EUR from budget text. Returns 0 if unknown."""
    text = text.strip()
    # "Moins de 500 €" -> 0 (below threshold)
    if re.search(r"moins\s+de\s+500", text, re.IGNORECASE):
        return 0
    # Extract numbers from text like "500 € à 1 000 €", "1 000 € à 10 000 €", "10 000 € et plus"
    numbers = re.findall(r"([\d\s]+)\s*€", text)
    if numbers:
        # Take the first (lowest) number
        raw = numbers[0].replace(" ", "").replace("\u202f", "").replace("\xa0", "")
        try:
            return int(raw)
        except ValueError:
            pass
    # "et plus" without clear number
    if "et plus" in text.lower():
        return 10000
    return 0


def budget_is_ok(budget_text: str) -> bool:
    """Return True if the budget is >= 500 EUR."""
    return parse_budget(budget_text) >= 500


def matches_keywords(text: str) -> list[str]:
    """Return list of matched keywords found in text (case insensitive).

    Uses word-boundary matching for all keywords to avoid false positives
    (e.g. "ia" in "materiau", "api" in "capital", "agent" in "inter-agences").
    """
    text_lower = text.lower()
    found = []
    for kw in MATCH_KEYWORDS:
        pattern = r"(?<![a-zA-Zéèêëàâùûôîïç])" + re.escape(kw) + r"(?![a-zA-Zéèêëàâùûôîïç])"
        if re.search(pattern, text_lower):
            found.append(kw)
    return found


def extract_project_id(href: str) -> str | None:
    """Extract project ID from href like /projects/480355-some-slug."""
    m = re.search(r"/projects/(\d+)", href)
    return m.group(1) if m else None


def parse_projects(html: str) -> list[dict]:
    """Parse project listings from the Codeur.com projects page HTML.

    Projects are in <div class="... card ..." id="project-XXXXX"> containers.
    The title link is inside the card, budget/offres/date are in the card text.
    """
    soup = BeautifulSoup(html, "html.parser")
    projects = []

    # Cards have id="project-XXXXX" and class containing "card"
    cards = soup.find_all("div", id=re.compile(r"^project-\d+"))
    for card in cards:
        # Extract project ID from the card's id attribute
        card_id = card.get("id", "")
        pid_match = re.search(r"project-(\d+)", card_id)
        if not pid_match:
            continue
        project_id = pid_match.group(1)

        # Title: find the <a> link to /projects/XXXXX-...
        link = card.find("a", href=re.compile(r"^/projects/\d+"))
        if not link:
            continue
        href = link.get("href", "")
        title = link.get_text(strip=True)

        # Full card text for field extraction and keyword matching
        card_text = card.get_text(" ", strip=True)

        # Budget: "Moins de 500 €", "500 € à 1 000 €", "1 000 € à 10 000 €", "10 000 € et plus"
        budget_match = re.search(
            r"((?:Moins de\s+)?\d[\d\s\xa0]*€(?:\s*[àa]\s*\d[\d\s\xa0]*€)?(?:\s*et plus)?)",
            card_text,
        )
        budget = budget_match.group(1).strip() if budget_match else "Non specifie"

        # Proposals: "X offres"
        proposals_match = re.search(r"(\d+)\s*offres?", card_text)
        nb_proposals = proposals_match.group(1) if proposals_match else "0"

        # Posted date: last "Il y a ..." in the card
        all_dates = re.findall(r"Il y a\s+\S+(?:\s+\S+)?", card_text)
        posted = all_dates[-1].strip() if all_dates else "Date inconnue"

        url = CODEUR_BASE_URL + href

        projects.append({
            "id": project_id,
            "title": title,
            "budget": budget,
            "nb_proposals": nb_proposals,
            "posted": posted,
            "url": url,
            "full_text": card_text,
        })

    return projects


def send_telegram(bot_token: str, chat_id: str, message: str) -> bool:
    """Send a Telegram message. Returns True on success."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "disable_web_page_preview": True,
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, json=payload, timeout=15)
            if resp.ok:
                return True
            logger.warning("Telegram API error (attempt %d): %s", attempt, resp.text)
        except requests.RequestException as e:
            logger.warning("Telegram send attempt %d failed: %s", attempt, e)
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY_SECONDS)
    logger.error("Failed to send Telegram message after %d attempts", MAX_RETRIES)
    return False


def format_telegram_message(project: dict) -> str:
    """Format a project into a Telegram alert message."""
    return (
        f"\U0001f514 Nouveau projet Codeur.com\n"
        f"\U0001f4cb {project['title']}\n"
        f"\U0001f4b0 {project['budget']}\n"
        f"\U0001f465 {project['nb_proposals']} propositions\n"
        f"\U0001f517 {project['url']}\n"
        f"\U0001f4c5 {project['posted']}"
    )


def save_to_db(projects: list[dict], applied_pids: set[str]) -> None:
    """Save matched projects into the codeur_projects SQLite table."""
    if not projects:
        return
    try:
        conn = sqlite3.connect(str(DB_PATH))
        c = conn.cursor()
        ts = datetime.now().isoformat()
        for p in projects:
            keywords = ",".join(p.get("matched_kw", []))
            score = len(p.get("matched_kw", [])) * 10
            c.execute(
                "INSERT OR IGNORE INTO codeur_projects "
                "(pid, title, budget, offers_count, keywords, score, scanned_at, applied) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    p["id"],
                    p["title"],
                    p["budget"],
                    int(p.get("nb_proposals", 0)),
                    keywords,
                    score,
                    ts,
                    1 if p["id"] in applied_pids else 0,
                ),
            )
        conn.commit()
        conn.close()
        logger.info("Saved %d project(s) to SQLite", len(projects))
    except Exception as e:
        logger.error("Failed to save projects to SQLite: %s", e)


def run_check(dry_run: bool = False) -> int:
    """Run a single check cycle. Returns number of new matching projects found."""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    if not dry_run and (not bot_token or not chat_id):
        logger.error("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set. Use --dry-run to test without Telegram.")
        return 0

    seen = load_seen_projects()
    all_projects: dict[str, dict] = {}
    matched_projects: list[dict] = []
    new_matches = 0

    # Scrape multiple pages of the main listing (Codeur.com search param
    # does not filter server-side, so we scrape the paginated listing and
    # filter locally with keywords).
    pages_to_scrape = PAGES_TO_SCRAPE
    for page_num in range(1, pages_to_scrape + 1):
        url = f"{CODEUR_BASE_URL}/projects?page={page_num}"
        logger.info("Fetching project listing page %d/%d", page_num, pages_to_scrape)
        html = fetch_page(url)
        if not html:
            continue

        projects = parse_projects(html)
        logger.info("  Parsed %d project(s) from page %d", len(projects), page_num)

        for p in projects:
            if p["id"] not in all_projects:
                all_projects[p["id"]] = p

        # Stop early if we got fewer projects than expected (last page)
        if len(projects) < 10:
            break

    logger.info("Total unique projects scraped: %d", len(all_projects))

    for pid, project in all_projects.items():
        # Skip already seen
        if pid in seen:
            continue

        # Check budget >= 500
        if not budget_is_ok(project["budget"]):
            logger.debug("Skipping %s — budget too low: %s", pid, project["budget"])
            seen[pid] = {"title": project["title"], "reason": "budget_low", "seen_at": datetime.now().isoformat()}
            continue

        # Check keyword match in title + description
        searchable = f"{project['title']} {project['full_text']}"
        matched_kw = matches_keywords(searchable)
        if not matched_kw:
            logger.debug("Skipping %s — no keyword match: %s", pid, project["title"])
            seen[pid] = {"title": project["title"], "reason": "no_keyword", "seen_at": datetime.now().isoformat()}
            continue

        # We have a match
        logger.info("NEW MATCH: [%s] %s (budget: %s, keywords: %s)", pid, project["title"], project["budget"], ", ".join(matched_kw))
        project["matched_kw"] = matched_kw
        matched_projects.append(project)
        new_matches += 1

        msg = format_telegram_message(project)

        if dry_run:
            print("\n" + msg + "\n")
        else:
            send_telegram(bot_token, chat_id, msg)

        seen[pid] = {
            "title": project["title"],
            "budget": project["budget"],
            "keywords": matched_kw,
            "notified": True,
            "seen_at": datetime.now().isoformat(),
        }

    # Save matched projects to SQLite
    applied_pids: set[str] = set()  # TODO: populate from codeur_offers table if needed
    save_to_db(matched_projects, applied_pids)

    save_seen_projects(seen)
    logger.info("Check complete. New matches: %d", new_matches)
    return new_matches


def main():
    parser = argparse.ArgumentParser(description="Monitor Codeur.com for AI/automation projects")
    parser.add_argument("--once", action="store_true", help="Run a single check and exit")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_MINUTES, help="Check interval in minutes (default: 30)")
    parser.add_argument("--dry-run", action="store_true", help="Print matches to stdout instead of sending Telegram")
    args = parser.parse_args()

    logger.info("=== codeur-veille started (mode: %s, interval: %dm) ===", "once" if args.once else "daemon", args.interval)

    if args.once:
        run_check(dry_run=args.dry_run)
    else:
        # Daemon mode
        while True:
            try:
                run_check(dry_run=args.dry_run)
            except Exception as e:
                logger.exception("Unexpected error during check cycle: %s", e)
            logger.info("Next check in %d minutes...", args.interval)
            time.sleep(args.interval * 60)


if __name__ == "__main__":
    main()
