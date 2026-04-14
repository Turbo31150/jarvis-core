#!/usr/bin/env python3
"""JARVIS Embedding Store — Persistent embedding storage with cosine similarity search"""

import redis
import json
import time
import struct
import math
import requests

r = redis.Redis(decode_responses=True)
r_bytes = redis.Redis(decode_responses=False)

STORE_PREFIX = "jarvis:embeddings:"
META_PREFIX = "jarvis:embeddings:meta:"
INDEX_KEY = "jarvis:embeddings:index"
EMBED_URL = "http://192.168.1.113:1234/v1/embeddings"
EMBED_MODEL = "text-embedding-nomic-embed-text-v1.5"


def _pack(vector: list) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def _unpack(data: bytes) -> list:
    n = len(data) // 4
    return list(struct.unpack(f"{n}f", data))


def _cosine(a: list, b: list) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb + 1e-9)


def get_embedding(text: str) -> list | None:
    """Fetch embedding from M32 nomic model."""
    try:
        resp = requests.post(
            EMBED_URL, json={"model": EMBED_MODEL, "input": text}, timeout=10
        )
        data = resp.json()
        return data["data"][0]["embedding"]
    except Exception:
        return None


def store(doc_id: str, text: str, vector: list = None, metadata: dict = None) -> bool:
    """Store text + embedding. Fetches embedding from M32 if not provided."""
    if vector is None:
        vector = get_embedding(text)
    if vector is None:
        return False
    # Store vector as packed binary
    r_bytes.set(f"{STORE_PREFIX}{doc_id}", _pack(vector))
    r_bytes.expire(f"{STORE_PREFIX}{doc_id}", 86400 * 7)
    # Store metadata as JSON
    meta = {
        "text": text[:500],
        "metadata": metadata or {},
        "dim": len(vector),
        "ts": time.time(),
    }
    r.setex(f"{META_PREFIX}{doc_id}", 86400 * 7, json.dumps(meta))
    r.sadd(INDEX_KEY, doc_id)
    return True


def store_with_mock(doc_id: str, text: str, metadata: dict = None) -> bool:
    """Store using a deterministic mock vector (for testing without M32)."""
    import hashlib

    seed = int(hashlib.md5(text.encode()).hexdigest()[:8], 16)
    import random

    rng = random.Random(seed)
    vector = [rng.gauss(0, 1) for _ in range(384)]
    norm = math.sqrt(sum(x * x for x in vector))
    vector = [x / norm for x in vector]
    return store(doc_id, text, vector, metadata)


def search(
    query_text: str = None,
    query_vector: list = None,
    top_k: int = 5,
    metadata_filter: dict = None,
) -> list:
    """Search by text or vector. Returns top-k most similar docs."""
    if query_vector is None and query_text:
        query_vector = get_embedding(query_text)
    if query_vector is None:
        # Mock vector for query
        import hashlib
        import random
        import math

        seed = int(hashlib.md5((query_text or "").encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)
        query_vector = [rng.gauss(0, 1) for _ in range(384)]
        norm = math.sqrt(sum(x * x for x in query_vector))
        query_vector = [x / norm for x in query_vector]

    doc_ids = list(r.smembers(INDEX_KEY))
    scores = []
    for doc_id in doc_ids:
        raw = r_bytes.get(f"{STORE_PREFIX}{doc_id}")
        meta_raw = r.get(f"{META_PREFIX}{doc_id}")
        if not raw or not meta_raw:
            r.srem(INDEX_KEY, doc_id)
            continue
        meta = json.loads(meta_raw)
        if metadata_filter:
            skip = any(
                meta.get("metadata", {}).get(k) != v for k, v in metadata_filter.items()
            )
            if skip:
                continue
        vec = _unpack(raw)
        if len(vec) == len(query_vector):
            sim = _cosine(query_vector, vec)
            scores.append(
                {
                    "id": doc_id,
                    "score": round(sim, 4),
                    "text": meta["text"][:100],
                    "metadata": meta.get("metadata", {}),
                }
            )

    return sorted(scores, key=lambda x: x["score"], reverse=True)[:top_k]


def delete(doc_id: str):
    r_bytes.delete(f"{STORE_PREFIX}{doc_id}")
    r.delete(f"{META_PREFIX}{doc_id}")
    r.srem(INDEX_KEY, doc_id)


def stats() -> dict:
    total = r.scard(INDEX_KEY)
    return {"total_docs": total, "index_key": INDEX_KEY, "embed_model": EMBED_MODEL}


if __name__ == "__main__":
    docs = [
        (
            "doc_gpu",
            "GPU temperature monitoring and thermal management",
            {"category": "infra"},
        ),
        (
            "doc_llm",
            "LLM routing and model selection for inference tasks",
            {"category": "ai"},
        ),
        (
            "doc_trade",
            "Bitcoin trading signals and market analysis",
            {"category": "trading"},
        ),
        ("doc_redis", "Redis caching and session management", {"category": "infra"}),
        (
            "doc_agent",
            "Autonomous agent planning and tool execution",
            {"category": "ai"},
        ),
    ]
    for doc_id, text, meta in docs:
        store_with_mock(doc_id, text, meta)

    print(f"Stored {stats()['total_docs']} docs")
    for query in ["AI model inference", "infrastructure monitoring"]:
        results = search(query_text=query, top_k=2)
        print(f"\nQuery: '{query}'")
        for res in results:
            print(f"  [{res['score']:.3f}] {res['id']}: {res['text'][:50]}")
