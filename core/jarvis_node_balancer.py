#!/usr/bin/env python3
"""JARVIS Node Balancer — Distribute workloads across cluster nodes with health-aware routing"""

import redis
import json
import time
import requests

r = redis.Redis(decode_responses=True)

NODES = {
    "M1": {"url": "http://192.168.1.85:1234", "max_concurrent": 3, "weight": 1},
    "M2": {"url": "http://192.168.1.26:1234", "max_concurrent": 5, "weight": 3},
    "M32": {"url": "http://192.168.1.113:1234", "max_concurrent": 4, "weight": 2},
    "OL1": {"url": "http://127.0.0.1:11434", "max_concurrent": 6, "weight": 2},
}

LOAD_PREFIX = "jarvis:balancer:load:"
STATS_KEY = "jarvis:balancer:stats"


def _get_load(node: str) -> int:
    return int(r.get(f"{LOAD_PREFIX}{node}") or 0)


def _incr_load(node: str):
    r.incr(f"{LOAD_PREFIX}{node}")
    r.expire(f"{LOAD_PREFIX}{node}", 300)


def _decr_load(node: str):
    v = _get_load(node)
    if v > 0:
        r.decr(f"{LOAD_PREFIX}{node}")


def _is_available(node: str) -> bool:
    load = _get_load(node)
    max_c = NODES[node]["max_concurrent"]
    state = r.hget(f"jarvis:cb:{node.lower()}", "state") or "closed"
    return load < max_c and state != "open"


def select_node(strategy: str = "least_loaded", exclude: list = None) -> str | None:
    """Select a node. Strategies: least_loaded, weighted, round_robin."""
    available = [n for n in NODES if _is_available(n) and n not in (exclude or [])]
    if not available:
        # Fallback: pick least overloaded
        available = [n for n in NODES if n not in (exclude or [])]
    if not available:
        return None

    if strategy == "least_loaded":
        return min(available, key=lambda n: _get_load(n) / NODES[n]["max_concurrent"])
    elif strategy == "weighted":
        import random

        weights = [NODES[n]["weight"] for n in available]
        return random.choices(available, weights=weights)[0]
    elif strategy == "round_robin":
        idx = int(r.incr("jarvis:balancer:rr_idx")) % len(available)
        return available[idx]
    return available[0]


def acquire(node: str) -> bool:
    """Mark node as having +1 active task."""
    if not _is_available(node):
        return False
    _incr_load(node)
    r.hincrby(STATS_KEY, f"{node}:acquired", 1)
    return True


def release(node: str, success: bool = True):
    """Release a task slot from node."""
    _decr_load(node)
    key = f"{node}:ok" if success else f"{node}:err"
    r.hincrby(STATS_KEY, key, 1)


def probe_node(node: str, timeout: int = 3) -> dict:
    """Check if node is reachable."""
    url = NODES[node]["url"]
    t0 = time.perf_counter()
    try:
        endpoint = f"{url}/api/tags" if "11434" in url else f"{url}/v1/models"
        resp = requests.get(endpoint, timeout=timeout)
        lat = round((time.perf_counter() - t0) * 1000)
        ok = resp.status_code == 200
        r.setex(f"jarvis:node:{node}:status", 120, "up" if ok else "degraded")
        r.setex(
            f"jarvis:balancer:probe:{node}", 120, json.dumps({"ok": ok, "lat_ms": lat})
        )
        return {"node": node, "ok": ok, "lat_ms": lat}
    except Exception as e:
        lat = round((time.perf_counter() - t0) * 1000)
        r.setex(f"jarvis:node:{node}:status", 120, "down")
        return {"node": node, "ok": False, "lat_ms": lat, "error": str(e)[:60]}


def cluster_status() -> dict:
    result = {}
    for node in NODES:
        probe_raw = r.get(f"jarvis:balancer:probe:{node}")
        probe = json.loads(probe_raw) if probe_raw else {}
        result[node] = {
            "load": _get_load(node),
            "max": NODES[node]["max_concurrent"],
            "available": _is_available(node),
            "last_probe": probe,
        }
    return result


def stats() -> dict:
    s = r.hgetall(STATS_KEY)
    return {k: int(v) for k, v in s.items()}


if __name__ == "__main__":
    # Probe all nodes
    print("Probing nodes...")
    for node in NODES:
        p = probe_node(node, timeout=2)
        icon = "✅" if p["ok"] else "❌"
        print(f"  {icon} {node}: {p.get('lat_ms', '?')}ms {p.get('error', '')}")

    # Simulate load balancing
    print("\nLoad balancing simulation:")
    for i in range(8):
        node = select_node("least_loaded")
        if node:
            acquire(node)
            print(f"  Task {i + 1} → {node} (load={_get_load(node)})")
            if i % 3 == 0:
                release(node, True)

    print(f"\nCluster: {cluster_status()}")
