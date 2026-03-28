"""NetworkOperator — network health monitoring agent."""

import json
from datetime import datetime

from core.network.health import (
    check_dns,
    check_port,
    conflict_detect,
    full_report as _full_report,
    latency_map as _latency_map,
    ping,
    port_scan,
    SERVICES,
)


class NetworkOperator:
    """Agent that wraps core.network.health for structured reporting."""

    def scan_ports(self) -> dict:
        """Scan all known JARVIS services and return port status."""
        results = port_scan()
        up = sum(1 for v in results.values() if v["up"])
        return {
            "timestamp": datetime.now().isoformat(),
            "services": results,
            "summary": {"total": len(results), "up": up, "down": len(results) - up},
        }

    def check_dns(self, hosts: list[str] | None = None) -> dict:
        """Resolve one or more hostnames and report success/failure."""
        hosts = hosts or ["google.com", "github.com", "api.anthropic.com"]
        results = {h: check_dns(h) for h in hosts}
        return {
            "timestamp": datetime.now().isoformat(),
            "dns": results,
            "ok": all(results.values()),
        }

    def latency_map(self) -> dict:
        """Return ping latency (ms) for every cluster node."""
        raw = _latency_map()
        return {
            "timestamp": datetime.now().isoformat(),
            "latency_ms": raw,
            "ok": all(v is not None for v in raw.values()),
        }

    def full_report(self) -> dict:
        """Aggregate ports + DNS + latency + conflicts into one report."""
        report = _full_report()
        report["conflicts"] = conflict_detect()
        up = sum(1 for v in report["services"].values() if v["up"])
        total = len(report["services"])
        report["summary"] = {
            "services_up": up,
            "services_down": total - up,
            "dns_ok": report["dns"],
            "gateway": report["gateway"],
            "conflicts": len(report["conflicts"]),
        }
        return report

    def detect_conflicts(self) -> dict:
        """Detect port conflicts on the host."""
        conflicts = conflict_detect()
        return {
            "timestamp": datetime.now().isoformat(),
            "conflicts": conflicts,
            "ok": len(conflicts) == 0,
        }


if __name__ == "__main__":
    op = NetworkOperator()
    print(json.dumps(op.full_report(), indent=2, default=str))
