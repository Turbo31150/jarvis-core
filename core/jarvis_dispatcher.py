#!/usr/bin/env python3
"""JARVIS Intelligent Dispatcher — Route tasks to optimal OpenClaw agent"""

import subprocess
import json
import re
import sys

# Routing table: keywords → agent
# Routing table — validé par M2/deepseek-r1 + OpenRouter/Nemotron-30B
ROUTING = {
    "trading|trade|crypto|mexc|bitcoin|signal|position|market|futures|long|short|perp|hyperliquid": "omega-trading-agent",
    "gpu|vram|temperature|thermal|nvidia|cuda|crash gpu": "cluster-mgr",
    "linkedin|post|content|publication|social": "social-growth",
    "codeur|mission|freelance|proposal|devis|client": "services-business",
    "telegram|notification|alert|message|notify": "comms",
    "security|audit|port|vulnerability|breach|scan": "omega-security-agent",
    "code|python|bug|refactor|test|fonction|debug": "omega-dev-agent",
    "memory|consolidation|mémoire|recall|knowledge": "data-pipeline",
    "system|service|boot|process|systemd|infra": "omega-system-agent",
    "voice|tts|stt|audio|speech|parole": "voice-engine",
    "sql|sqlite|database|query|data|etl": "data-pipeline",
    "monitor|health|status|metric|perf": "monitoring",
    "deploy|docker|container|install": "deployment",
    "linux|apt|package|kernel|sysctl|bash": "linux-admin",
    "analyse|research|compare|benchmark|study": "omega-analysis-agent",
    "doc|documentation|readme|wiki|explain": "omega-docs-agent",
    "schedule|cron|planif|routine|periodic": "automation",
    "route|dispatch|llm.*routing|orchestrat": "dispatch",
    "windows|powershell|registry|wsl": "win-admin",
}


def route(task: str) -> str:
    task_lower = task.lower()
    for pattern, agent in ROUTING.items():
        if re.search(pattern, task_lower):
            return agent
    return "misc-ops"  # fallback


def dispatch(task: str, agent: str = None, dry_run: bool = False) -> dict:
    if agent is None:
        agent = route(task)

    cmd = ["openclaw", "run", "--agent", agent, "--prompt", task]

    if dry_run:
        return {"agent": agent, "cmd": " ".join(cmd), "dry_run": True}

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return {
            "agent": agent,
            "task": task[:100],
            "success": result.returncode == 0,
            "output": result.stdout[:500],
            "error": result.stderr[:200] if result.returncode != 0 else None,
        }
    except subprocess.TimeoutExpired:
        return {"agent": agent, "success": False, "error": "timeout 120s"}
    except Exception as e:
        return {"agent": agent, "success": False, "error": str(e)}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: jarvis_dispatcher.py 'task description' [--dry]")
        sys.exit(1)

    task = " ".join(a for a in sys.argv[1:] if not a.startswith("--"))
    dry = "--dry" in sys.argv

    result = dispatch(task, dry_run=dry)
    print(json.dumps(result, indent=2, ensure_ascii=False))
