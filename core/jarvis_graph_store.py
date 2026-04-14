#!/usr/bin/env python3
"""JARVIS Graph Store — Simple graph of relationships between JARVIS entities"""

import redis
import json
from datetime import datetime

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:graph"


def add_node(node_id: str, node_type: str, properties: dict = None):
    data = {"id": node_id, "type": node_type, "props": json.dumps(properties or {}), "created": datetime.now().isoformat()[:19]}
    r.hset(f"{PREFIX}:nodes:{node_id}", mapping=data)
    r.sadd(f"{PREFIX}:type:{node_type}", node_id)


def add_edge(from_id: str, to_id: str, relation: str, weight: float = 1.0):
    edge_key = f"{PREFIX}:edges:{from_id}:{relation}"
    r.zadd(edge_key, {to_id: weight})
    r.sadd(f"{PREFIX}:relations", relation)
    # Reverse index
    r.sadd(f"{PREFIX}:reverse:{to_id}", f"{from_id}:{relation}")


def get_neighbors(node_id: str, relation: str = None) -> list:
    if relation:
        raw = r.zrange(f"{PREFIX}:edges:{node_id}:{relation}", 0, -1, withscores=True)
        return [{"node": n, "weight": w, "relation": relation} for n, w in raw]
    # All relations
    results = []
    for key in r.scan_iter(f"{PREFIX}:edges:{node_id}:*"):
        rel = key.split(":")[-1]
        for n, w in r.zrange(key, 0, -1, withscores=True):
            results.append({"node": n, "weight": w, "relation": rel})
    return results


def find_path(start: str, end: str, max_depth: int = 3) -> list:
    """BFS path finding"""
    from collections import deque
    queue = deque([[start]])
    visited = {start}
    while queue:
        path = queue.popleft()
        node = path[-1]
        if node == end:
            return path
        if len(path) > max_depth:
            continue
        for neighbor in get_neighbors(node):
            n = neighbor["node"]
            if n not in visited:
                visited.add(n)
                queue.append(path + [n])
    return []


def build_jarvis_topology():
    """Build the JARVIS system topology graph"""
    # Nodes
    for node, ntype, props in [
        ("M2",  "cluster_node",  {"url": "192.168.1.26:1234", "status": "up"}),
        ("M32", "cluster_node",  {"url": "192.168.1.113:1234", "status": "up"}),
        ("OL1", "cluster_node",  {"url": "127.0.0.1:11434", "status": "up"}),
        ("llm_router",    "service", {"port": 8767}),
        ("api_gateway",   "service", {"port": 8767}),
        ("dashboard",     "service", {"port": 8765}),
        ("redis",         "service", {"port": 6379}),
        ("task_queue",    "component", {}),
        ("circuit_breaker", "component", {}),
    ]:
        add_node(node, ntype, props)

    # Edges
    for f, t, rel, w in [
        ("api_gateway",   "llm_router",      "uses",    1.0),
        ("api_gateway",   "redis",            "depends", 1.0),
        ("llm_router",    "M2",               "routes_to", 0.5),
        ("llm_router",    "M32",              "routes_to", 0.5),
        ("llm_router",    "OL1",              "routes_to", 0.3),
        ("llm_router",    "circuit_breaker",  "uses",    1.0),
        ("llm_router",    "task_queue",       "uses",    0.5),
        ("dashboard",     "redis",            "reads",   1.0),
        ("M2",            "M32",              "failover", 1.0),
        ("M32",           "OL1",              "failover", 0.8),
    ]:
        add_edge(f, t, rel, w)

    return {"nodes": 9, "edges": 10}


def stats() -> dict:
    nodes = sum(1 for _ in r.scan_iter(f"{PREFIX}:nodes:*"))
    edges = sum(1 for _ in r.scan_iter(f"{PREFIX}:edges:*:*"))
    relations = list(r.smembers(f"{PREFIX}:relations"))
    return {"nodes": nodes, "edges": edges, "relations": relations}


if __name__ == "__main__":
    topo = build_jarvis_topology()
    print(f"Topology built: {topo}")
    print(f"Stats: {stats()}")
    neighbors = get_neighbors("llm_router")
    print(f"llm_router neighbors: {[(n['node'], n['relation']) for n in neighbors]}")
    path = find_path("api_gateway", "OL1")
    print(f"Path api_gateway→OL1: {path}")
