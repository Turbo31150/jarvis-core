"""BrowserOSOperator — control BrowserOS via its CLI."""

import json
import subprocess
from pathlib import Path


CLI_PATH = Path.home() / ".browseros" / "bin" / "browseros-cli"


class BrowserOSOperator:
    """Drive BrowserOS browser automation through its local CLI."""

    def __init__(self, cli: str | Path = CLI_PATH):
        self.cli = str(cli)

    def _run(self, *args: str) -> str:
        result = subprocess.run(
            [self.cli, *args],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            raise RuntimeError(f"browseros error: {result.stderr.strip()}")
        return result.stdout.strip()

    def _json(self, *args: str) -> list | dict:
        raw = self._run(*args, "--json")
        return json.loads(raw) if raw else {}

    # -- page operations -----------------------------------------------------

    def list_pages(self) -> list[dict]:
        return self._json("pages", "list")

    def navigate(self, page_id: str, url: str) -> str:
        return self._run("pages", "navigate", page_id, url)

    def snapshot(self, page_id: str) -> dict:
        return self._json("pages", "snapshot", page_id)

    def click(self, page_id: str, element_id: str) -> str:
        return self._run("pages", "click", page_id, element_id)

    def fill(self, page_id: str, element_id: str, text: str) -> str:
        return self._run("pages", "fill", page_id, element_id, text)

    # -- group operations ----------------------------------------------------

    def list_groups(self) -> list[dict]:
        return self._json("groups", "list")

    # -- health --------------------------------------------------------------

    def health(self) -> dict:
        try:
            raw = self._run("status")
            return {"ok": True, "status": raw}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def summary(self) -> str:
        lines = ["=== BrowserOS Status ==="]
        h = self.health()
        lines.append(f"  Server: {'UP' if h['ok'] else 'DOWN'}")
        try:
            pages = self.list_pages()
            lines.append(f"  Open tabs: {len(pages)}")
            for p in pages[:10]:
                title = p.get("title", "untitled")[:60]
                lines.append(f"    - [{p.get('id', '?')}] {title}")
        except Exception:
            lines.append("  Could not list pages")
        try:
            groups = self.list_groups()
            lines.append(f"  Tab groups: {len(groups)}")
        except Exception:
            pass
        return "\n".join(lines)
