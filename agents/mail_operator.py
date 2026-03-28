"""MailOperator — IMAP mail agent (read-only) with classification."""

import email
import imaplib
import json
import re
import sqlite3
from datetime import datetime
from email.header import decode_header
from pathlib import Path
from typing import Optional

DB_PATH = Path.home() / "IA" / "Core" / "jarvis" / "data" / "mail_metadata.db"

CATEGORIES = {
    "github":     [r"github\.com", r"noreply@github", r"\[GitHub\]"],
    "infra":      [r"alert", r"monitoring", r"nagios", r"grafana", r"prometheus", r"uptime"],
    "client":     [r"invoice", r"facture", r"devis", r"client"],
    "error":      [r"error", r"exception", r"failure", r"crash", r"500"],
    "security":   [r"security", r"breach", r"CVE", r"vulnerability", r"auth"],
    "newsletter": [r"unsubscribe", r"newsletter", r"digest", r"weekly"],
}


def _decode_str(raw: Optional[str]) -> str:
    if raw is None:
        return ""
    parts = decode_header(raw)
    decoded = []
    for data, charset in parts:
        if isinstance(data, bytes):
            decoded.append(data.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(data)
    return " ".join(decoded)


class MailOperator:
    """Read-only IMAP agent with classification and action extraction."""

    def __init__(self):
        self._imap: Optional[imaplib.IMAP4_SSL] = None
        self._ensure_db()

    def _ensure_db(self) -> None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS mail_metadata ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  timestamp TEXT, message_id TEXT, sender TEXT,"
            "  subject TEXT, category TEXT, actions TEXT"
            ")"
        )
        conn.commit()
        conn.close()

    def connect(self, host: str, user: str, password: str, port: int = 993) -> dict:
        """Connect to IMAP server (SSL)."""
        try:
            self._imap = imaplib.IMAP4_SSL(host, port)
            status, data = self._imap.login(user, password)
            return {"connected": True, "host": host, "user": user}
        except Exception as exc:
            self._imap = None
            return {"connected": False, "error": str(exc)}

    def inbox_summary(self, count: int = 20) -> list[dict]:
        """Fetch the latest N messages from INBOX (headers only)."""
        if not self._imap:
            return [{"error": "not connected"}]
        self._imap.select("INBOX", readonly=True)
        _, data = self._imap.search(None, "ALL")
        ids = data[0].split()[-count:] if data[0] else []
        messages = []
        for mid in reversed(ids):
            _, msg_data = self._imap.fetch(mid, "(RFC822.HEADER)")
            if msg_data[0] is None:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            subject = _decode_str(msg.get("Subject"))
            sender = _decode_str(msg.get("From"))
            date = msg.get("Date", "")
            cat = self.classify(subject + " " + sender)
            entry = {"subject": subject, "from": sender, "date": date, "category": cat}
            messages.append(entry)
            self._save_metadata(msg.get("Message-ID", ""), sender, subject, cat)
        return messages

    def classify(self, text: str) -> str:
        """Classify text into a mail category."""
        lower = text.lower()
        for cat, patterns in CATEGORIES.items():
            for p in patterns:
                if re.search(p, lower):
                    return cat
        return "other"

    def extract_actions(self, body: str) -> list[str]:
        """Extract actionable items from a mail body."""
        actions = []
        patterns = [
            r"(?:please|merci de|could you|peux-tu|action required)[:\s]+(.+)",
            r"(?:TODO|FIXME|ACTION)[:\s]+(.+)",
            r"(?:deadline|date limite|before|avant)[:\s]+(.+)",
        ]
        for p in patterns:
            for m in re.finditer(p, body, re.IGNORECASE):
                actions.append(m.group(1).strip()[:200])
        return actions

    def daily_digest(self) -> dict:
        """Build a categorized digest of today's inbox."""
        messages = self.inbox_summary(count=50)
        by_cat: dict[str, list] = {}
        for m in messages:
            cat = m.get("category", "other")
            by_cat.setdefault(cat, []).append(m)
        return {
            "timestamp": datetime.now().isoformat(),
            "total": len(messages),
            "by_category": {k: len(v) for k, v in by_cat.items()},
            "messages": by_cat,
        }

    def search(self, query: str) -> list[dict]:
        """Search INBOX by subject keyword (IMAP SUBJECT search)."""
        if not self._imap:
            return [{"error": "not connected"}]
        self._imap.select("INBOX", readonly=True)
        _, data = self._imap.search(None, "SUBJECT", f'"{query}"')
        ids = data[0].split() if data[0] else []
        results = []
        for mid in reversed(ids[:20]):
            _, msg_data = self._imap.fetch(mid, "(RFC822.HEADER)")
            if msg_data[0] is None:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            results.append({
                "subject": _decode_str(msg.get("Subject")),
                "from": _decode_str(msg.get("From")),
                "date": msg.get("Date", ""),
            })
        return results

    def _save_metadata(self, message_id: str, sender: str, subject: str, category: str) -> None:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute(
            "INSERT INTO mail_metadata (timestamp, message_id, sender, subject, category, actions) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (datetime.now().isoformat(), message_id, sender, subject, category, ""),
        )
        conn.commit()
        conn.close()

    def disconnect(self) -> None:
        if self._imap:
            try:
                self._imap.logout()
            except Exception:
                pass
            self._imap = None


if __name__ == "__main__":
    op = MailOperator()
    print("MailOperator initialized (connect with .connect(host, user, password))")
    print(f"Categories: {list(CATEGORIES.keys())}")
    print(f"classify('GitHub notification') → {op.classify('GitHub notification')}")
    print(f"classify('Server error 500')   → {op.classify('Server error 500')}")
    print(f"extract_actions('Please review the PR before Friday') → {op.extract_actions('Please review the PR before Friday')}")
