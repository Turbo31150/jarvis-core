"""GitHubOperator — manage GitHub repos via the gh CLI."""

import json
import subprocess
from datetime import datetime


class GitHubOperator:
    """Interact with GitHub through the gh CLI."""

    def __init__(self, default_owner: str = "Turbo31150"):
        self.owner = default_owner

    @staticmethod
    def _run(args: list[str]) -> str:
        result = subprocess.run(
            ["gh"] + args,
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"gh error: {result.stderr.strip()}")
        return result.stdout.strip()

    def list_repos(self, limit: int = 30) -> list[dict]:
        raw = self._run([
            "repo", "list", self.owner,
            "--json", "name,description,stargazerCount,updatedAt,isPrivate",
            "--limit", str(limit),
        ])
        return json.loads(raw) if raw else []

    def get_issues(self, repo: str, limit: int = 20) -> list[dict]:
        raw = self._run([
            "issue", "list",
            "--repo", f"{self.owner}/{repo}",
            "--json", "number,title,state,author,createdAt,labels",
            "--limit", str(limit),
        ])
        return json.loads(raw) if raw else []

    def get_prs(self, repo: str, limit: int = 20) -> list[dict]:
        raw = self._run([
            "pr", "list",
            "--repo", f"{self.owner}/{repo}",
            "--json", "number,title,state,author,createdAt,headRefName",
            "--limit", str(limit),
        ])
        return json.loads(raw) if raw else []

    def scan_todos(self, repo: str) -> list[str]:
        try:
            raw = self._run([
                "search", "code",
                "--repo", f"{self.owner}/{repo}",
                "--json", "path,textMatches",
                "TODO OR FIXME",
            ])
            return json.loads(raw) if raw else []
        except RuntimeError:
            return []

    def daily_summary(self) -> str:
        repos = self.list_repos(limit=10)
        lines = [f"=== GitHub Daily Summary — {datetime.now():%Y-%m-%d} ===", ""]
        for r in repos:
            name = r["name"]
            vis = "private" if r.get("isPrivate") else "public"
            lines.append(f"  {name} ({vis}) — {r.get('description', 'no desc')}")
            try:
                issues = self.get_issues(name, limit=5)
                prs = self.get_prs(name, limit=5)
                if issues:
                    lines.append(f"    Issues: {len(issues)} open")
                if prs:
                    lines.append(f"    PRs: {len(prs)} open")
            except RuntimeError:
                lines.append("    (could not fetch details)")
            lines.append("")
        return "\n".join(lines)
