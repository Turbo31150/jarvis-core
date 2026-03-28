"""ContainerOperator — Docker management agent via CLI."""

import json
import subprocess
from datetime import datetime
from typing import Optional


class ContainerOperator:
    """Manage Docker containers, images, and volumes."""

    @staticmethod
    def _run(args: list[str], timeout: int = 15) -> str:
        result = subprocess.run(
            ["docker"] + args,
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            raise RuntimeError(f"docker error: {result.stderr.strip()}")
        return result.stdout.strip()

    def list_containers(self, all: bool = True) -> list[dict]:
        """List containers with status, image, and ports."""
        fmt = '{"name":{{json .Names}},"image":{{json .Image}},"status":{{json .Status}},"ports":{{json .Ports}}}'
        flag = "-a" if all else ""
        raw = self._run(["ps", flag, "--format", fmt] if flag else ["ps", "--format", fmt])
        if not raw:
            return []
        return [json.loads(line) for line in raw.splitlines() if line.strip()]

    def get_logs(self, name: str, lines: int = 50) -> dict:
        """Return the last N lines of logs for a container."""
        try:
            logs = self._run(["logs", "--tail", str(lines), name], timeout=10)
            return {"container": name, "lines": lines, "logs": logs}
        except RuntimeError as exc:
            return {"container": name, "error": str(exc)}

    def health(self) -> dict:
        """Overall Docker health: daemon up, container counts."""
        try:
            info_raw = self._run(["info", "--format", "{{json .}}"], timeout=10)
            info = json.loads(info_raw)
            containers = self.list_containers(all=True)
            running = sum(1 for c in containers if "Up" in c.get("status", ""))
            return {
                "timestamp": datetime.now().isoformat(),
                "daemon": True,
                "containers_total": len(containers),
                "containers_running": running,
                "containers_stopped": len(containers) - running,
                "server_version": info.get("ServerVersion", "unknown"),
            }
        except Exception as exc:
            return {"daemon": False, "error": str(exc)}

    def detect_crash_loops(self) -> list[dict]:
        """Detect containers that are restarting repeatedly."""
        containers = self.list_containers(all=True)
        loops = []
        for c in containers:
            status = c.get("status", "")
            if "Restarting" in status or status.count("Exited") and "ago" not in status:
                loops.append(c)
        return loops

    def unused_images(self) -> list[dict]:
        """List dangling / unused images."""
        fmt = '{"id":{{json .ID}},"repo":{{json .Repository}},"tag":{{json .Tag}},"size":{{json .Size}}}'
        raw = self._run(["images", "--filter", "dangling=true", "--format", fmt])
        if not raw:
            return []
        return [json.loads(line) for line in raw.splitlines() if line.strip()]

    def orphan_volumes(self) -> list[str]:
        """List volumes not attached to any container."""
        raw = self._run(["volume", "ls", "--filter", "dangling=true", "--format", "{{.Name}}"])
        return [v for v in raw.splitlines() if v.strip()] if raw else []

    def snapshot(self) -> dict:
        """Full snapshot: containers, crash loops, unused images, orphan volumes."""
        return {
            "timestamp": datetime.now().isoformat(),
            "health": self.health(),
            "crash_loops": self.detect_crash_loops(),
            "unused_images": self.unused_images(),
            "orphan_volumes": self.orphan_volumes(),
        }


if __name__ == "__main__":
    op = ContainerOperator()
    print(json.dumps(op.snapshot(), indent=2, default=str))
