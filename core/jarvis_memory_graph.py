#!/usr/bin/env python3
"""JARVIS Memory Graph — Knowledge graph with entities, relations, and temporal context"""

import redis
import json
import time
import hashlib

r = redis.Redis(decode_responses=True)

ENTITY_PREFIX = "jarvis:mgraph:entity:"
RELATION_PREFIX = "jarvis:mgraph:rel:"
INDEX_KEY = "jarvis:mgraph:entities"
STATS_KEY = "jarvis:mgraph:stats"


def _eid(name: str, entity_type: str) -> str:
    return hashlib.md5(f"{entity_type}:{name}".encode()).hexdigest()[:16]


def add_entity(
    name: str, entity_type: str, properties: dict = None, ttl: int = None
) -> str:
    """Add or update an entity in the graph."""
    eid = _eid(name, entity_type)
    entity = {
        "id": eid,
        "name": name,
        "type": entity_type,
        "properties": properties or {},
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    key = f"{ENTITY_PREFIX}{eid}"
    if ttl:
        r.setex(key, ttl, json.dumps(entity))
    else:
        r.set(key, json.dumps(entity))
    r.sadd(INDEX_KEY, eid)
    r.sadd(f"jarvis:mgraph:type:{entity_type}", eid)
    r.hincrby(STATS_KEY, "entities_added", 1)
    return eid


def add_relation(
    from_name: str,
    from_type: str,
    relation: str,
    to_name: str,
    to_type: str,
    weight: float = 1.0,
    properties: dict = None,
) -> str:
    """Add a directed relation between two entities."""
    from_id = _eid(from_name, from_type)
    to_id = _eid(to_name, to_type)
    rel_key = f"{RELATION_PREFIX}{from_id}:{relation}:{to_id}"
    rel = {
        "from_id": from_id,
        "from_name": from_name,
        "from_type": from_type,
        "to_id": to_id,
        "to_name": to_name,
        "to_type": to_type,
        "relation": relation,
        "weight": weight,
        "properties": properties or {},
        "ts": time.time(),
    }
    r.setex(rel_key, 86400 * 7, json.dumps(rel))
    # Adjacency index
    r.zadd(f"jarvis:mgraph:out:{from_id}", {f"{relation}:{to_id}": weight})
    r.zadd(f"jarvis:mgraph:in:{to_id}", {f"{relation}:{from_id}": weight})
    r.hincrby(STATS_KEY, "relations_added", 1)
    return rel_key


def get_entity(name: str, entity_type: str) -> dict | None:
    eid = _eid(name, entity_type)
    raw = r.get(f"{ENTITY_PREFIX}{eid}")
    return json.loads(raw) if raw else None


def get_relations(name: str, entity_type: str, direction: str = "out") -> list:
    """Get all relations from/to an entity."""
    eid = _eid(name, entity_type)
    key = f"jarvis:mgraph:{'out' if direction == 'out' else 'in'}:{eid}"
    raw = r.zrange(key, 0, -1, withscores=True)
    results = []
    for item, score in raw:
        relation, other_id = item.split(":", 1)
        other_raw = r.get(f"{ENTITY_PREFIX}{other_id}")
        other = json.loads(other_raw) if other_raw else {"id": other_id}
        results.append({"relation": relation, "entity": other, "weight": score})
    return results


def get_by_type(entity_type: str) -> list:
    eids = r.smembers(f"jarvis:mgraph:type:{entity_type}")
    return [
        json.loads(r.get(f"{ENTITY_PREFIX}{eid}"))
        for eid in eids
        if r.exists(f"{ENTITY_PREFIX}{eid}")
    ]


def search_path(
    from_name: str, from_type: str, to_name: str, to_type: str, max_depth: int = 3
) -> list:
    """BFS shortest path between two entities."""
    from_id = _eid(from_name, from_type)
    to_id = _eid(to_name, to_type)
    if from_id == to_id:
        return [from_name]
    visited = {from_id}
    queue = [(from_id, [from_name])]
    for _ in range(max_depth):
        next_queue = []
        for current_id, path in queue:
            for item, _ in r.zrange(
                f"jarvis:mgraph:out:{current_id}", 0, -1, withscores=True
            ):
                _, neighbor_id = item.split(":", 1)
                if neighbor_id == to_id:
                    neighbor_raw = r.get(f"{ENTITY_PREFIX}{neighbor_id}")
                    neighbor_name = (
                        json.loads(neighbor_raw)["name"]
                        if neighbor_raw
                        else neighbor_id
                    )
                    return path + [neighbor_name]
                if neighbor_id not in visited:
                    visited.add(neighbor_id)
                    neighbor_raw = r.get(f"{ENTITY_PREFIX}{neighbor_id}")
                    neighbor_name = (
                        json.loads(neighbor_raw)["name"]
                        if neighbor_raw
                        else neighbor_id
                    )
                    next_queue.append((neighbor_id, path + [neighbor_name]))
        queue = next_queue
    return []


def stats() -> dict:
    s = r.hgetall(STATS_KEY)
    total = r.scard(INDEX_KEY)
    return {"total_entities": total, **{k: int(v) for k, v in s.items()}}


if __name__ == "__main__":
    # Build JARVIS cluster knowledge graph
    for node, ntype, props in [
        ("M2", "node", {"ip": "192.168.1.26", "vram_gb": 24}),
        ("M32", "node", {"ip": "192.168.1.113", "vram_gb": 10}),
        ("OL1", "node", {"ip": "127.0.0.1", "type": "ollama"}),
        ("qwen3.5-35b", "model", {"params": "35B", "task": "code"}),
        ("mistral-7b", "model", {"params": "7B", "task": "instruct"}),
        ("deepseek-r1", "model", {"params": "8B", "task": "reasoning"}),
        ("gemma3:4b", "model", {"params": "4B", "task": "fast"}),
        ("api_gateway", "service", {"port": 8767}),
        ("llm_router", "service", {"port": 0}),
    ]:
        add_entity(node, ntype, props)

    # Relations
    add_relation("M2", "node", "hosts", "qwen3.5-35b", "model", 1.0)
    add_relation("M2", "node", "hosts", "deepseek-r1", "model", 0.9)
    add_relation("M32", "node", "hosts", "mistral-7b", "model", 1.0)
    add_relation("OL1", "node", "hosts", "gemma3:4b", "model", 1.0)
    add_relation("api_gateway", "service", "routes_to", "llm_router", "service", 1.0)
    add_relation("llm_router", "service", "uses", "M2", "node", 0.8)
    add_relation("llm_router", "service", "uses", "M32", "node", 0.6)
    add_relation("llm_router", "service", "uses", "OL1", "node", 0.4)

    print(f"Graph: {stats()['total_entities']} entities")
    path = search_path("api_gateway", "service", "gemma3:4b", "model")
    print(f"Path api_gateway → gemma3:4b: {' → '.join(path)}")
    rels = get_relations("llm_router", "service", "out")
    print(
        f"llm_router outbound: {[r_['relation'] + ':' + r_['entity']['name'] for r_ in rels]}"
    )
