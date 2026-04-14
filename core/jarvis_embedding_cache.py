#!/usr/bin/env python3
"""JARVIS Embedding Cache — Cache text embeddings to avoid redundant computation"""

import redis
import hashlib
import json
import time
import struct
from datetime import datetime

r = redis.Redis(decode_responses=False)  # binary for embeddings
r_text = redis.Redis(decode_responses=True)

PREFIX = "jarvis:embeddings"
DEFAULT_TTL = 86400  # 24h


def _key(text: str, model: str = "default") -> str:
    h = hashlib.sha256(f"{model}:{text.strip().lower()}".encode()).hexdigest()[:16]
    return f"{PREFIX}:{h}"


def get(text: str, model: str = "default") -> list | None:
    raw = r.get(_key(text, model))
    if raw:
        r_text.hincrby(f"{PREFIX}:stats", "hits", 1)
        # Deserialize float32 vector
        n = len(raw) // 4
        return list(struct.unpack(f"{n}f", raw))
    r_text.hincrby(f"{PREFIX}:stats", "misses", 1)
    return None


def store(text: str, embedding: list, model: str = "default", ttl: int = DEFAULT_TTL):
    key = _key(text, model)
    # Serialize as float32 binary
    packed = struct.pack(f"{len(embedding)}f", *embedding)
    r.setex(key.encode(), ttl, packed)
    r_text.hincrby(f"{PREFIX}:stats", "stored", 1)


def get_or_compute(text: str, compute_fn, model: str = "default") -> tuple[list, bool]:
    """Return (embedding, was_cached)"""
    cached = get(text, model)
    if cached is not None:
        return cached, True
    embedding = compute_fn(text)
    if embedding:
        store(text, embedding, model)
    return embedding, False


def cosine_similarity(a: list, b: list) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


def stats() -> dict:
    data = r_text.hgetall(f"{PREFIX}:stats")
    hits = int(data.get("hits", 0))
    misses = int(data.get("misses", 0))
    total = hits + misses
    return {
        "hits": hits,
        "misses": misses,
        "stored": int(data.get("stored", 0)),
        "hit_rate_pct": round(hits / total * 100, 1) if total else 0.0,
    }


if __name__ == "__main__":
    import random
    # Simulate 768-dim embeddings
    def fake_embed(text):
        random.seed(hash(text) % 2**32)
        return [random.gauss(0, 1) for _ in range(768)]

    texts = ["Hello world", "Redis is fast", "Python is great", "Hello world"]  # last is duplicate
    for text in texts:
        emb, cached = get_or_compute(text, fake_embed)
        print(f"  '{text[:30]}': {'HIT' if cached else 'MISS'} ({len(emb)} dims)")

    # Test similarity
    e1 = get("Hello world")
    e2 = get("Redis is fast")
    sim = cosine_similarity(e1, e2) if e1 and e2 else 0
    print(f"Similarity('Hello world', 'Redis is fast'): {sim:.3f}")
    print(f"Stats: {stats()}")
