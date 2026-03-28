"""VoiceRouter — maps voice commands to TaskRequest objects."""

import json
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.tasks.models import TaskRequest, TaskResult, TaskStatus, TaskPriority

DB_PATH = Path.home() / "IA" / "Core" / "jarvis" / "data" / "voice_commands.db"

# Pattern → (task_type, prompt_template, target_node)
VOICE_PATTERNS: list[tuple[str, str, str, Optional[str]]] = [
    (r"statut\s+r[eé]seau",   "network",  "network full_report",    "local"),
    (r"scan\s+codeur",         "code",     "codeur scan",            "M1"),
    (r"check\s+linkedin",      "browser",  "linkedin engage",        "browseros"),
    (r"rapport",               "analysis", "daily report",           "local"),
    (r"gpu\s+status",          "generic",  "gpu status check",       "local"),
    (r"sant[eé]\s+cluster",    "network",  "cluster health check",   "local"),
    (r"trading\s+scan",        "analysis", "trading market scan",    "M3"),
    (r"mail",                  "generic",  "mail daily digest",      "local"),
    (r"docker\s+status",       "generic",  "container health check", "local"),
    (r"base\s+de\s+donn[eé]es","sql",      "sql health check",      "local"),
]


class VoiceRouter:
    """Parse voice text into TaskRequests and log results to SQLite."""

    def __init__(self):
        self._ensure_db()

    def _ensure_db(self) -> None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS voice_commands ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  timestamp TEXT, raw_text TEXT, intent TEXT,"
            "  task_type TEXT, result TEXT"
            ")"
        )
        conn.commit()
        conn.close()

    def parse_intent(self, text: str) -> TaskRequest:
        """Convert a voice phrase into a structured TaskRequest."""
        lower = text.lower().strip()
        for pattern, task_type, prompt, node in VOICE_PATTERNS:
            if re.search(pattern, lower):
                return TaskRequest(
                    prompt=prompt,
                    task_type=task_type,
                    target_node=node,
                    priority=TaskPriority.MEDIUM,
                    metadata={"source": "voice", "raw_text": text},
                )
        return TaskRequest(
            prompt=text,
            task_type="generic",
            metadata={"source": "voice", "raw_text": text, "unmatched": True},
        )

    def execute(self, text: str) -> TaskResult:
        """Parse intent, log command, and return a TaskResult stub."""
        req = self.parse_intent(text)
        result = TaskResult(
            request_id=req.id,
            status=TaskStatus.COMPLETED,
            output={"intent": req.task_type, "prompt": req.prompt, "node": req.target_node},
            node=req.target_node,
        )
        self.log_command(text, req.task_type, result.status.value)
        return result

    def list_commands(self) -> list[dict]:
        """Return all known voice patterns and their mappings."""
        return [
            {"pattern": p, "task_type": t, "prompt": pr, "node": n}
            for p, t, pr, n in VOICE_PATTERNS
        ]

    def log_command(self, text: str, intent: str, result: str) -> None:
        """Persist a voice command to SQLite."""
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute(
            "INSERT INTO voice_commands (timestamp, raw_text, intent, task_type, result) "
            "VALUES (?, ?, ?, ?, ?)",
            (datetime.now().isoformat(), text, intent, intent, result),
        )
        conn.commit()
        conn.close()


if __name__ == "__main__":
    router = VoiceRouter()
    for phrase in ["statut réseau", "scan codeur", "check linkedin", "rapport", "hello"]:
        req = router.parse_intent(phrase)
        print(f"  '{phrase}' → type={req.task_type}, prompt={req.prompt}, node={req.target_node}")
    print("\nAvailable commands:")
    print(json.dumps(router.list_commands(), indent=2))
