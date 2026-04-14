#!/usr/bin/env python3
"""JARVIS Knowledge Base — Structured knowledge store with tagging, versioning, and search"""

import redis
import json
import time
import re

r = redis.Redis(decode_responses=True)

DOC_PREFIX = "jarvis:kb:doc:"
TAG_PREFIX = "jarvis:kb:tag:"
SEARCH_PREFIX = "jarvis:kb:search:"
ALL_DOCS_KEY = "jarvis:kb:all"
STATS_KEY = "jarvis:kb:stats"


def store(
    doc_id: str,
    title: str,
    content: str,
    tags: list = None,
    source: str = "manual",
    ttl: int = None,
) -> dict:
    """Store a knowledge document."""
    existing_raw = r.get(f"{DOC_PREFIX}{doc_id}")
    version = 1
    if existing_raw:
        existing = json.loads(existing_raw)
        version = existing.get("version", 1) + 1
        # Remove old tags
        for tag in existing.get("tags", []):
            r.srem(f"{TAG_PREFIX}{tag}", doc_id)

    doc = {
        "id": doc_id,
        "title": title,
        "content": content,
        "tags": tags or [],
        "source": source,
        "version": version,
        "created_at": time.time(),
        "word_count": len(content.split()),
    }
    if ttl:
        r.setex(f"{DOC_PREFIX}{doc_id}", ttl, json.dumps(doc))
    else:
        r.set(f"{DOC_PREFIX}{doc_id}", json.dumps(doc))
    r.sadd(ALL_DOCS_KEY, doc_id)

    # Tag index
    for tag in tags or []:
        r.sadd(f"{TAG_PREFIX}{tag}", doc_id)

    # Search index: tokenize title + content
    words = set(re.findall(r"\b\w{3,}\b", (title + " " + content).lower()))
    for word in words:
        r.sadd(f"{SEARCH_PREFIX}{word}", doc_id)

    r.hincrby(STATS_KEY, "stored", 1)
    return doc


def get(doc_id: str) -> dict | None:
    raw = r.get(f"{DOC_PREFIX}{doc_id}")
    return json.loads(raw) if raw else None


def search(query: str, tags: list = None, limit: int = 10) -> list:
    """Full-text search across title + content."""
    words = re.findall(r"\b\w{3,}\b", query.lower())
    if not words:
        return []

    # Intersect doc sets for all query words
    candidate_sets = [
        r.smembers(f"{SEARCH_PREFIX}{w}")
        for w in words
        if r.exists(f"{SEARCH_PREFIX}{w}")
    ]
    if not candidate_sets:
        return []

    candidates = (
        set.intersection(*candidate_sets)
        if len(candidate_sets) > 1
        else candidate_sets[0]
    )

    # Tag filter
    if tags:
        for tag in tags:
            tagged = r.smembers(f"{TAG_PREFIX}{tag}")
            candidates = candidates & tagged

    results = []
    for doc_id in list(candidates)[: limit * 2]:
        doc = get(doc_id)
        if doc:
            # Simple relevance: word matches in title weighted 3x
            title_matches = sum(1 for w in words if w in doc["title"].lower())
            content_matches = sum(1 for w in words if w in doc["content"].lower())
            score = title_matches * 3 + content_matches
            results.append({**doc, "_score": score})

    results.sort(key=lambda x: x["_score"], reverse=True)
    r.hincrby(STATS_KEY, "searches", 1)
    return results[:limit]


def get_by_tag(tag: str) -> list:
    doc_ids = r.smembers(f"{TAG_PREFIX}{tag}")
    return [d for d_id in doc_ids if (d := get(d_id))]


def delete(doc_id: str) -> bool:
    doc = get(doc_id)
    if not doc:
        return False
    for tag in doc.get("tags", []):
        r.srem(f"{TAG_PREFIX}{tag}", doc_id)
    words = set(
        re.findall(r"\b\w{3,}\b", (doc["title"] + " " + doc["content"]).lower())
    )
    for word in words:
        r.srem(f"{SEARCH_PREFIX}{word}", doc_id)
    r.delete(f"{DOC_PREFIX}{doc_id}")
    r.srem(ALL_DOCS_KEY, doc_id)
    return True


def stats() -> dict:
    s = r.hgetall(STATS_KEY)
    total = r.scard(ALL_DOCS_KEY)
    tags = [k.replace(TAG_PREFIX, "") for k in r.keys(f"{TAG_PREFIX}*")]
    return {"total_docs": total, "tags": len(tags), **{k: int(v) for k, v in s.items()}}


if __name__ == "__main__":
    docs = [
        (
            "gpu_guide",
            "GPU Thermal Management",
            "Monitor GPU temperatures. Alert above 82°C. Use VRAM efficiently.",
            ["infra", "gpu"],
        ),
        (
            "llm_routing",
            "LLM Routing Strategy",
            "Route code tasks to M2 qwen35b. Fast tasks to OL1 gemma3. Reasoning to deepseek-r1.",
            ["ai", "llm"],
        ),
        (
            "redis_ops",
            "Redis Operations Guide",
            "Use Redis streams for events. Sorted sets for priority queues. Hashes for stats.",
            ["infra", "redis"],
        ),
        (
            "trading_signals",
            "Trading Signal Processing",
            "BTC ETH signals via deepseek-r1. Confidence threshold 0.7. Risk management required.",
            ["trading"],
        ),
        (
            "cluster_failover",
            "Cluster Failover Procedures",
            "M3 → OL1 → M1 → M2 cascade. Check circuit breakers before routing.",
            ["infra", "cluster"],
        ),
    ]
    for doc_id, title, content, tags in docs:
        store(doc_id, title, content, tags)

    print(f"KB: {stats()['total_docs']} docs, {stats()['tags']} tags")
    for query in ["GPU temperature monitoring", "LLM routing model"]:
        results = search(query, limit=2)
        print(f"\nSearch '{query}':")
        for d in results:
            print(f"  [{d['_score']}] {d['id']}: {d['title']}")
