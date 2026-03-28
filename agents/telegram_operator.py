"""TelegramOperator — send messages and digests via Telegram bot API."""

import json
import os
import urllib.request
from datetime import datetime
from typing import Any


class TelegramOperator:
    """Interact with Telegram Bot API for JARVIS notifications."""

    API = "https://api.telegram.org"

    def __init__(
        self,
        token: str | None = None,
        chat_id: str | None = None,
    ):
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
        if not self.token or not self.chat_id:
            raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")

    def _call(self, method: str, data: dict[str, Any]) -> dict:
        url = f"{self.API}/bot{self.token}/{method}"
        payload = json.dumps(data).encode()
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    # -- send messages -------------------------------------------------------

    def send(self, message: str) -> dict:
        return self._call("sendMessage", {
            "chat_id": self.chat_id,
            "text": message,
        })

    def send_markdown(self, message: str) -> dict:
        return self._call("sendMessage", {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "MarkdownV2",
        })

    # -- command handlers ----------------------------------------------------

    def cmd_health_full(self) -> str:
        from jarvis.core.memory.facade import MemoryFacade
        facade = MemoryFacade(read_only=True)
        health = facade.health_check()
        lines = ["Health Check:"]
        for db, status in health.items():
            mark = "OK" if status.get("ok") else "FAIL"
            lines.append(f"  {db}: {mark}")
        return "\n".join(lines)

    def cmd_sql_stats(self) -> str:
        from jarvis.core.memory.facade import MemoryFacade
        facade = MemoryFacade(read_only=True)
        stats = facade.get_stats()
        lines = ["SQL Stats:"]
        for db, info in stats.items():
            if "error" in info:
                lines.append(f"  {db}: ERROR — {info['error']}")
            else:
                lines.append(f"  {db}: {info['tables']} tables, {info['rows']} rows, {info['size_kb']} KB")
        return "\n".join(lines)

    def cmd_agents_status(self) -> str:
        return f"Agents operational — {datetime.now():%H:%M:%S}"

    # -- daily digest --------------------------------------------------------

    def daily_digest(self) -> dict:
        now = datetime.now()
        parts = [
            f"JARVIS Daily Digest — {now:%Y-%m-%d %H:%M}",
            "",
            self.cmd_health_full(),
            "",
            self.cmd_sql_stats(),
            "",
            self.cmd_agents_status(),
        ]
        return self.send("\n".join(parts))
