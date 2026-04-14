#!/usr/bin/env python3
"""JARVIS Cluster Rebalancer — Detect load imbalance and suggest/execute model migrations"""

import redis
import json
import time

r = redis.Redis(decode_responses=True)

STATS_KEY = "jarvis:rebalance:stats"
LOG_KEY = "jarvis:rebalance:log"

NODES = {
    "m1": {"vram_total_mb": 40960, "max_rps": 20, "priority": 9},
    "m2": {"vram_total_mb": 24576, "max_rps": 15, "priority": 6},
    "m32": {"vram_total_mb": 10240, "max_rps": 12, "priority": 8},
    "ol1": {"vram_total_mb": 8192, "max_rps": 6, "priority": 5},
}


def _get_node_load(node: str) -> dict:
    """Read current load from Redis (populated by node_balancer/telemetry)."""
    raw = r.get(f"jarvis:balancer:node:{node}")
    if raw:
        state = json.loads(raw)
        total = NODES[node]["vram_total_mb"]
        used = state.get("vram_used_mb", 0)
        return {
            "node": node,
            "vram_used_mb": used,
            "vram_total_mb": total,
            "vram_pct": round(used / max(total, 1) * 100, 1),
            "active_requests": state.get("active_requests", 0),
            "health": state.get("health", 1.0),
        }
    return {
        "node": node,
        "vram_used_mb": 0,
        "vram_total_mb": NODES[node]["vram_total_mb"],
        "vram_pct": 0.0,
        "active_requests": 0,
        "health": 1.0,
    }


def _get_model_placements() -> dict:
    """Scan Redis for loaded model placements."""
    placements = {}
    for key in r.scan_iter("jarvis:gpusched:model:*"):
        raw = r.get(key)
        if raw:
            info = json.loads(raw)
            model = info.get("model", key.split(":")[-1])
            placements[model] = info
    return placements


def analyze() -> dict:
    """Compute cluster load balance report."""
    loads = {node: _get_node_load(node) for node in NODES}
    placements = _get_model_placements()

    vram_pcts = [l["vram_pct"] for l in loads.values()]
    mean_vram = sum(vram_pcts) / len(vram_pcts) if vram_pcts else 0
    max_vram = max(vram_pcts) if vram_pcts else 0
    min_vram = min(vram_pcts) if vram_pcts else 0
    imbalance = max_vram - min_vram

    overloaded = [n for n, l in loads.items() if l["vram_pct"] > 85]
    underloaded = [
        n for n, l in loads.items() if l["vram_pct"] < 30 and l["health"] > 0.5
    ]

    return {
        "loads": loads,
        "placements": placements,
        "mean_vram_pct": round(mean_vram, 1),
        "imbalance_pct": round(imbalance, 1),
        "overloaded": overloaded,
        "underloaded": underloaded,
        "needs_rebalance": imbalance > 30,
    }


def suggest_migrations(report: dict) -> list:
    """Generate migration suggestions to reduce imbalance."""
    suggestions = []
    overloaded = report["overloaded"]
    underloaded = report["underloaded"]

    if not overloaded or not underloaded:
        return suggestions

    placements = report["placements"]
    loads = report["loads"]

    for model, info in placements.items():
        src_node = info.get("node") or info.get("gpu_id", "")
        if str(src_node) not in overloaded and src_node not in overloaded:
            continue
        vram_mb = info.get("vram_mb", 0)

        for dst_node in underloaded:
            dst_load = loads[dst_node]
            free_mb = dst_load["vram_total_mb"] - dst_load["vram_used_mb"]
            if free_mb >= vram_mb:
                suggestions.append(
                    {
                        "action": "migrate",
                        "model": model,
                        "from": src_node,
                        "to": dst_node,
                        "vram_mb": vram_mb,
                        "reason": f"src overloaded ({loads.get(src_node, {}).get('vram_pct', 0):.0f}%), "
                        f"dst free {free_mb}MB",
                    }
                )
                break

    return suggestions


def execute_rebalance(dry_run: bool = True) -> dict:
    report = analyze()
    suggestions = suggest_migrations(report)

    log_entry = {
        "ts": time.time(),
        "dry_run": dry_run,
        "imbalance_pct": report["imbalance_pct"],
        "overloaded": report["overloaded"],
        "underloaded": report["underloaded"],
        "migrations": len(suggestions),
    }
    r.lpush(LOG_KEY, json.dumps(log_entry))
    r.ltrim(LOG_KEY, 0, 99)
    r.hincrby(STATS_KEY, "rebalance_runs", 1)
    if suggestions and not dry_run:
        r.hincrby(STATS_KEY, "migrations_executed", len(suggestions))

    return {
        "report": report,
        "suggestions": suggestions,
        "executed": not dry_run and bool(suggestions),
    }


def stats() -> dict:
    s = r.hgetall(STATS_KEY)
    return {k: int(v) for k, v in s.items()}


if __name__ == "__main__":
    result = execute_rebalance(dry_run=True)
    report = result["report"]

    print("Cluster Load Report:")
    for node, load in report["loads"].items():
        bar = "█" * int(load["vram_pct"] / 5) + "░" * (20 - int(load["vram_pct"] / 5))
        print(
            f"  {node:4s} {bar} {load['vram_pct']:5.1f}%  "
            f"health={load['health']:.2f}  active={load['active_requests']}"
        )

    print(
        f"\nImbalance: {report['imbalance_pct']:.1f}%  "
        f"needs_rebalance={report['needs_rebalance']}"
    )
    print(f"Overloaded: {report['overloaded']}")
    print(f"Underloaded: {report['underloaded']}")

    if result["suggestions"]:
        print("\nMigration suggestions:")
        for s in result["suggestions"]:
            print(
                f"  MOVE {s['model']} {s['from']} → {s['to']}  "
                f"({s['vram_mb']}MB)  {s['reason']}"
            )
    else:
        print("\nNo migrations needed.")

    print(f"\nStats: {stats()}")
