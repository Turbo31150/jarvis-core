"""JARVIS Security — Action policies, allowlists, audit."""
import time, sqlite3, os, json, logging
from enum import Enum
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("jarvis.security")

class ActionLevel(Enum):
    READ_ONLY = "read_only"
    CONFIRM_REQUIRED = "confirm_required"
    DANGEROUS = "dangerous"

SHELL_ALLOWLIST = {
    "github_operator": ["gh *"],
    "browseros_operator": ["browseros-cli *"],
    "network_operator": ["ping *", "curl *", "ss *", "ip *"],
    "sql_operator": ["sqlite3 *"],
    "container_operator": ["docker ps *", "docker logs *", "docker stats *"],
}

DANGEROUS_PATTERNS = [
    "rm -rf", "dd if=", "mkfs", "> /dev/", "chmod 777",
    "DROP TABLE", "DELETE FROM", "TRUNCATE",
    "git push --force", "git reset --hard",
]

@dataclass
class ActionPolicy:
    agent: str
    action: str
    level: ActionLevel
    requires_confirmation: bool = False

def check_action(agent: str, command: str) -> ActionPolicy:
    """Check if an action is allowed for an agent."""
    # Check dangerous patterns
    for pattern in DANGEROUS_PATTERNS:
        if pattern.lower() in command.lower():
            return ActionPolicy(agent, command, ActionLevel.DANGEROUS, requires_confirmation=True)
    # Check allowlist
    allowed = SHELL_ALLOWLIST.get(agent, [])
    for pattern in allowed:
        base = pattern.replace(" *", "")
        if command.startswith(base):
            return ActionPolicy(agent, command, ActionLevel.READ_ONLY)
    return ActionPolicy(agent, command, ActionLevel.CONFIRM_REQUIRED, requires_confirmation=True)

def audit_log(agent: str, action: str, result: str, db_path: str = "data/jarvis-master.db"):
    """Log action to audit trail."""
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, agent TEXT, action TEXT, result TEXT, timestamp TEXT)")
        conn.execute("INSERT INTO audit_log (agent, action, result, timestamp) VALUES (?,?,?,?)",
            (agent, action[:500], result[:200], time.strftime("%Y-%m-%dT%H:%M:%S")))
        conn.commit()
        conn.close()
    except: pass

def detect_secrets(text: str) -> list:
    """Detect potential secrets in text."""
    import re
    patterns = [
        (r'sk-[a-zA-Z0-9]{20,}', "API key (sk-)"),
        (r'ghp_[a-zA-Z0-9]{36}', "GitHub token"),
        (r'AKIA[0-9A-Z]{16}', "AWS key"),
        (r'[a-zA-Z0-9+/]{40}=', "Base64 secret"),
        (r'password\s*[=:]\s*\S+', "Password in config"),
    ]
    found = []
    for pattern, desc in patterns:
        if re.search(pattern, text):
            found.append(desc)
    return found
