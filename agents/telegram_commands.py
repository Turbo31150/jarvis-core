"""Telegram command handlers — standalone functions returning Markdown strings.

Each cmd_* function gathers data from core services and agents,
then formats it as a readable Markdown report suitable for Telegram.
"""

import subprocess
import time
from datetime import datetime
from typing import Optional


def _safe(fn, fallback="unavailable"):
    """Run *fn* and return its result; return *fallback* on any error."""
    try:
        return fn()
    except Exception as exc:
        return f"{fallback} ({exc})"


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# /health — full cluster health
# ---------------------------------------------------------------------------

def cmd_health_full() -> str:
    from core.services import ServiceRegistry
    from core.network.health import check_port, check_dns, check_gateway
    from core.memory.facade import MemoryFacade

    reg = ServiceRegistry()
    lines = [f"*JARVIS Health Report*  `{_ts()}`", ""]

    # Cluster nodes
    lines.append("*Cluster Nodes*")
    for svc in reg.list_all():
        up = check_port(svc.host, svc.port, timeout=2)
        mark = "UP" if up else "DOWN"
        lines.append(f"  {svc.name} ({svc.host}:{svc.port}) — {mark}")

    # GPU temps
    lines.append("")
    lines.append("*GPU*")
    gpu = _safe(lambda: subprocess.run(
        ["nvidia-smi", "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total",
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=5,
    ).stdout.strip())
    if gpu and gpu != "unavailable":
        for row in gpu.splitlines():
            lines.append(f"  {row.strip()}")
    else:
        lines.append(f"  {gpu}")

    # DB stats
    lines.append("")
    lines.append("*Databases*")
    try:
        facade = MemoryFacade(read_only=True)
        health = facade.health_check()
        for db, info in health.items():
            mark = "OK" if info.get("ok") else "FAIL"
            lines.append(f"  {db}: {mark}")
        facade.close_all()
    except Exception as exc:
        lines.append(f"  error: {exc}")

    # BrowserOS
    lines.append("")
    lines.append("*BrowserOS*")
    bos_up = check_port("127.0.0.1", 9222, timeout=2)
    lines.append(f"  CDP: {'UP' if bos_up else 'DOWN'}")

    # Network basics
    lines.append("")
    lines.append("*Network*")
    lines.append(f"  DNS: {'OK' if check_dns() else 'FAIL'}")
    gw = check_gateway()
    lines.append(f"  Gateway: {gw or 'unknown'}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /network — detailed network status
# ---------------------------------------------------------------------------

def cmd_network_status() -> str:
    from core.network.health import port_scan, check_dns, check_gateway, latency_map

    lines = [f"*Network Status*  `{_ts()}`", ""]

    # Port scan
    lines.append("*Port Scan*")
    results = port_scan()
    for name, info in results.items():
        mark = "OPEN" if info["up"] else "CLOSED"
        lines.append(f"  {name} ({info['host']}:{info['port']}) — {mark}")

    # DNS
    lines.append("")
    lines.append("*DNS Resolution*")
    for host in ["google.com", "github.com", "api.anthropic.com"]:
        ok = check_dns(host)
        lines.append(f"  {host}: {'OK' if ok else 'FAIL'}")

    # Gateway
    lines.append("")
    gw = check_gateway()
    lines.append(f"*Gateway*: {gw or 'unknown'}")

    # Latency map
    lines.append("")
    lines.append("*Latency Map*")
    lat = latency_map()
    for node, ms in lat.items():
        val = f"{ms:.1f} ms" if ms is not None else "timeout"
        lines.append(f"  {node}: {val}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /sql — database statistics
# ---------------------------------------------------------------------------

def cmd_sql_stats() -> str:
    from core.memory.facade import MemoryFacade

    facade = MemoryFacade(read_only=True)
    stats = facade.get_stats()
    facade.close_all()

    lines = [f"*SQL Stats*  `{_ts()}`", ""]
    for db, info in stats.items():
        if "error" in info:
            lines.append(f"*{db}*: ERROR — {info['error']}")
        else:
            lines.append(f"*{db}*: {info['tables']} tables, {info['rows']} rows, {info['size_kb']} KB")
            if info.get("detail"):
                for table, cnt in sorted(info["detail"].items()):
                    lines.append(f"  {table}: {cnt} rows")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /mail — inbox summary (placeholder — reads from DB if available)
# ---------------------------------------------------------------------------

def cmd_mail_summary() -> str:
    lines = [f"*Mail Summary*  `{_ts()}`", ""]
    try:
        from core.memory.facade import MemoryFacade
        facade = MemoryFacade(read_only=True)
        rows = facade.query("master",
            "SELECT * FROM mail_inbox ORDER BY received_at DESC LIMIT 10")
        facade.close_all()
        urgent = [r for r in rows if r.get("priority") == "urgent"]
        lines.append(f"Inbox: {len(rows)} recent messages")
        lines.append(f"Urgent: {len(urgent)}")
        for r in rows[:5]:
            subj = r.get("subject", "no subject")[:60]
            lines.append(f"  - {subj}")
    except Exception:
        lines.append("Mail data not available (no mail_inbox table)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /containers — Docker container status
# ---------------------------------------------------------------------------

def cmd_containers() -> str:
    lines = [f"*Containers*  `{_ts()}`", ""]
    try:
        ps = subprocess.run(
            ["docker", "ps", "-a", "--format",
             "{{.Names}}\t{{.Status}}\t{{.Ports}}"],
            capture_output=True, text=True, timeout=10,
        )
        if ps.returncode != 0:
            lines.append(f"docker error: {ps.stderr.strip()}")
            return "\n".join(lines)
        containers = ps.stdout.strip().splitlines()
        lines.append(f"Total: {len(containers)}")
        for c in containers:
            parts = c.split("\t")
            name = parts[0] if parts else "?"
            status = parts[1] if len(parts) > 1 else "unknown"
            restart = "CRASH" if "Restarting" in status else ""
            lines.append(f"  {name} — {status} {restart}")

        # Resource usage summary
        stats = subprocess.run(
            ["docker", "stats", "--no-stream", "--format",
             "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}"],
            capture_output=True, text=True, timeout=10,
        )
        if stats.returncode == 0 and stats.stdout.strip():
            lines.append("")
            lines.append("*Resource Usage*")
            for row in stats.stdout.strip().splitlines():
                lines.append(f"  {row}")
    except FileNotFoundError:
        lines.append("Docker not installed")
    except Exception as exc:
        lines.append(f"Error: {exc}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /tabs — BrowserOS open tabs
# ---------------------------------------------------------------------------

def cmd_browseros_tabs() -> str:
    from agents.browseros_operator import BrowserOSOperator

    lines = [f"*BrowserOS Tabs*  `{_ts()}`", ""]
    try:
        bos = BrowserOSOperator()
        pages = bos.list_pages()
        lines.append(f"Open tabs: {len(pages)}")
        for p in pages[:15]:
            title = p.get("title", "untitled")[:60]
            lines.append(f"  [{p.get('id', '?')}] {title}")
        groups = bos.list_groups()
        lines.append(f"Tab groups: {len(groups)}")
    except Exception as exc:
        lines.append(f"BrowserOS unavailable: {exc}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /agents — all agents and their status
# ---------------------------------------------------------------------------

def cmd_agents_status() -> str:
    agent_info = [
        ("GitHubOperator",   "agents.github_operator",   ["list_repos", "get_issues", "get_prs"]),
        ("BrowserOSOperator","agents.browseros_operator", ["list_pages", "navigate", "health"]),
        ("TelegramOperator", "agents.telegram_operator",  ["send", "send_markdown", "daily_digest"]),
        ("NetworkOperator",  "agents.network_operator",   ["scan_ports", "latency_map", "full_report"]),
        ("SQLOperator",      "agents.sql_operator",       ["get_stats", "query", "health_check"]),
    ]
    lines = [f"*Agents Status*  `{_ts()}`", ""]
    for name, module, methods in agent_info:
        try:
            mod = __import__(module, fromlist=[name])
            cls = getattr(mod, name)
            caps = ", ".join(methods)
            lines.append(f"  {name}: LOADED — [{caps}]")
        except Exception as exc:
            lines.append(f"  {name}: IMPORT FAIL — {exc}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /tasks — task queue from workflow_runs
# ---------------------------------------------------------------------------

def cmd_task_queue() -> str:
    import sqlite3
    from pathlib import Path

    db_path = Path.home() / "IA" / "Core" / "jarvis" / "data" / "jarvis-master.db"
    lines = [f"*Task Queue*  `{_ts()}`", ""]
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        for status in ["pending", "running", "completed", "failed"]:
            rows = conn.execute(
                "SELECT id, task_type, prompt, duration FROM workflow_runs "
                "WHERE status = ? ORDER BY created_at DESC LIMIT 5", (status,)
            ).fetchall()
            lines.append(f"*{status.upper()}* ({len(rows)})")
            for r in rows:
                prompt = (r["prompt"] or "")[:50]
                dur = f"{r['duration']:.1f}s" if r["duration"] else "-"
                lines.append(f"  [{r['id']}] {r['task_type']}: {prompt} ({dur})")
            lines.append("")
        conn.close()
    except Exception as exc:
        lines.append(f"Task queue unavailable: {exc}")
    return "\n".join(lines)
