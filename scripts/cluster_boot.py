#!/usr/bin/env python3
"""
JARVIS Cluster Boot Orchestrator
Healthcheck-only mode: reports service status without launching start commands.
"""

import asyncio
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum

import yaml
import aiohttp

# ── Status enum ─────────────────────────────────────────────────────────────


class Status(Enum):
    RUNNING = "✅ RUNNING"
    DEGRADED = "⚠️  DEGRADED"
    FAILED = "❌ FAILED"
    SKIPPED = "⏭️  SKIPPED"


@dataclass
class ServiceResult:
    machine: str
    name: str
    status: Status
    latency_ms: Optional[float] = None
    error: Optional[str] = None


# ── Healthcheck logic ────────────────────────────────────────────────────────


async def healthcheck(
    session: aiohttp.ClientSession,
    url: str,
    timeout: int,
    retries: int,
) -> Tuple[bool, Optional[float], Optional[str]]:
    """
    Try GET url up to `retries` times with exponential backoff (2s, 4s, 8s...).
    Returns (ok, latency_ms, error_msg).
    """
    delay = 2
    last_err = None
    for attempt in range(retries):
        t0 = time.monotonic()
        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                latency = (time.monotonic() - t0) * 1000
                if resp.status < 400:
                    return True, latency, None
                last_err = f"HTTP {resp.status}"
        except asyncio.TimeoutError:
            last_err = f"timeout ({timeout}s)"
        except aiohttp.ClientConnectorError:
            last_err = "connection refused"
        except Exception as e:
            last_err = str(e)[:60]

        if attempt < retries - 1:
            await asyncio.sleep(delay)
            delay *= 2

    return False, None, last_err


# ── Per-machine boot ─────────────────────────────────────────────────────────


async def boot_machine(
    machine_name: str,
    machine_cfg: dict,
    session: aiohttp.ClientSession,
) -> List[ServiceResult]:
    services = machine_cfg.get("services", {})
    results: Dict[str, ServiceResult] = {}
    failed_set: set = set()

    # Build dependency-ordered list (topological sort)
    ordered = _topo_sort(services)

    tasks = {}
    pending = list(ordered)
    done_events: Dict[str, asyncio.Event] = {s: asyncio.Event() for s in pending}

    async def check_service(name: str, cfg: dict):
        # Wait for all dependencies to complete first
        for dep in cfg.get("depends", []):
            ev = done_events.get(dep)
            if ev:
                await ev.wait()

        # Skip if any dependency failed
        skip = any(dep in failed_set for dep in cfg.get("depends", []))
        if skip:
            results[name] = ServiceResult(machine_name, name, Status.SKIPPED)
            done_events[name].set()
            return

        ok, latency, err = await healthcheck(
            session,
            cfg["health"],
            cfg.get("timeout", 15),
            cfg.get("retries", 3),
        )

        if ok:
            results[name] = ServiceResult(
                machine_name, name, Status.RUNNING, latency_ms=latency
            )
        else:
            failed_set.add(name)
            results[name] = ServiceResult(machine_name, name, Status.FAILED, error=err)

        done_events[name].set()

    # Launch all service checks concurrently (deps handled via events)
    coros = [check_service(name, cfg) for name, cfg in services.items()]
    await asyncio.gather(*coros)

    return [results[name] for name in services]  # preserve original order


def _topo_sort(services: dict) -> List[str]:
    """Simple topological sort by dependency depth."""
    visited = set()
    order = []

    def visit(name):
        if name in visited:
            return
        visited.add(name)
        for dep in services.get(name, {}).get("depends", []):
            if dep in services:
                visit(dep)
        order.append(name)

    for name in services:
        visit(name)
    return order


# ── Main orchestrator ────────────────────────────────────────────────────────


async def main(config_path: str) -> int:
    cfg_file = Path(config_path)
    if not cfg_file.exists():
        print(f"[ERROR] Config not found: {config_path}", file=sys.stderr)
        return 1

    with open(cfg_file) as f:
        config = yaml.safe_load(f)

    machines = config.get("machines", {})
    if not machines:
        print("[ERROR] No machines defined in config.", file=sys.stderr)
        return 1

    print(f"[JARVIS] Cluster boot — checking {len(machines)} machine(s)...\n")

    connector = aiohttp.TCPConnector(limit=20, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        machine_tasks = [
            boot_machine(name, cfg, session) for name, cfg in machines.items()
        ]
        all_results_nested = await asyncio.gather(*machine_tasks)

    all_results: List[ServiceResult] = []
    for results in all_results_nested:
        all_results.extend(results)

    # ── Print table ──────────────────────────────────────────────────────────
    col_m = max(len(r.machine) for r in all_results) + 2
    col_s = max(len(r.name) for r in all_results) + 2
    col_st = 14
    col_lat = 12
    col_err = 30

    header = (
        f"{'MACHINE':<{col_m}} {'SERVICE':<{col_s}} {'STATUS':<{col_st}} "
        f"{'LATENCY':<{col_lat}} {'ERROR'}"
    )
    sep = "─" * (col_m + col_s + col_st + col_lat + col_err + 4)

    print(sep)
    print(header)
    print(sep)

    for r in all_results:
        latency = f"{r.latency_ms:.0f} ms" if r.latency_ms else "—"
        error = r.error or ""
        print(
            f"{r.machine:<{col_m}} {r.name:<{col_s}} {r.status.value:<{col_st}} "
            f"{latency:<{col_lat}} {error}"
        )

    print(sep)

    # Summary
    counts = {s: 0 for s in Status}
    for r in all_results:
        counts[r.status] += 1

    summary = "  ".join(f"{s.value}: {counts[s]}" for s in Status if counts[s] > 0)
    print(f"\nSummary: {summary}\n")

    # Return non-zero if any FAILED
    return 1 if counts[Status.FAILED] > 0 else 0


if __name__ == "__main__":
    config_path = (
        sys.argv[1]
        if len(sys.argv) > 1
        else str(Path(__file__).parent.parent / "config" / "cluster_boot.yaml")
    )
    rc = asyncio.run(main(config_path))
    sys.exit(rc)
