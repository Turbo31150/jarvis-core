#!/usr/bin/env python3
"""JARVIS Vector Search — Similarity search using stored float32 embeddings"""

import redis
import json
import math
import time

r = redis.Redis(decode_responses=False)
r_str = redis.Redis(decode_responses=True)

EMBED_PREFIX = "jarvis:embed:"
INDEX_KEY = "jarvis:embed:index"


def _cosine(a: list, b: list) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb + 1e-9)


def store_embedding(key: str, text: str, vector: list, metadata: dict = None):
    """Store a text embedding with metadata."""
    payload = {
        "text": text[:500],
        "vector": vector,
        "metadata": metadata or {},
        "ts": time.time(),
    }
    r_str.set(f"{EMBED_PREFIX}{key}", json.dumps(payload))
    r_str.sadd(INDEX_KEY, key)


def search(query_vector: list, top_k: int = 5, filter_meta: dict = None) -> list:
    """Find top-k most similar embeddings by cosine similarity."""
    keys = list(r_str.smembers(INDEX_KEY))
    scores = []
    for k in keys:
        raw = r_str.get(f"{EMBED_PREFIX}{k}")
        if not raw:
            continue
        data = json.loads(raw)
        if filter_meta:
            skip = any(
                data.get("metadata", {}).get(fk) != fv for fk, fv in filter_meta.items()
            )
            if skip:
                continue
        sim = _cosine(query_vector, data["vector"])
        scores.append(
            {
                "key": k,
                "score": round(sim, 4),
                "text": data["text"],
                "metadata": data.get("metadata", {}),
            }
        )
    scores.sort(key=lambda x: x["score"], reverse=True)
    return scores[:top_k]


def delete_embedding(key: str):
    r_str.delete(f"{EMBED_PREFIX}{key}")
    r_str.srem(INDEX_KEY, key)


def stats() -> dict:
    total = r_str.scard(INDEX_KEY)
    return {"total_embeddings": total, "index_key": INDEX_KEY}


if __name__ == "__main__":
    import random

    dim = 16
    # Seed some test embeddings
    for i in range(10):
        vec = [random.gauss(0, 1) for _ in range(dim)]
        store_embedding(
            f"doc_{i}", f"Document number {i}", vec, {"category": "test", "id": i}
        )
    # Query
    query = [random.gauss(0, 1) for _ in range(dim)]
    results = search(query, top_k=3)
    print(f"Vector Search — {stats()['total_embeddings']} docs indexed")
    for r_ in results:
        print(f"  [{r_['score']:.3f}] {r_['key']}: {r_['text'][:40]}")
