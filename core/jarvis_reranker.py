#!/usr/bin/env python3
"""JARVIS Reranker — Re-rank LLM/search results by quality, relevance, and recency"""

import redis
import time
import re
import math

r = redis.Redis(decode_responses=True)
STATS_KEY = "jarvis:reranker:stats"


def _score_relevance(text: str, query: str) -> float:
    """BM25-inspired relevance score."""
    if not query or not text:
        return 0.0
    query_terms = re.findall(r"\b\w{2,}\b", query.lower())
    text_lower = text.lower()
    words = text_lower.split()
    doc_len = max(len(words), 1)
    avg_len = 150  # assumed average
    k1, b = 1.5, 0.75
    score = 0.0
    for term in query_terms:
        tf = text_lower.count(term)
        idf = math.log(1 + 1 / (0.5 + tf))
        bm25 = idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * doc_len / avg_len))
        score += bm25
    return round(min(score / max(len(query_terms), 1), 1.0), 4)


def _score_quality(text: str) -> float:
    """Score response quality: length, structure, no preamble."""
    if not text:
        return 0.0
    score = 0.5  # base

    # Good length
    wc = len(text.split())
    if 20 <= wc <= 500:
        score += 0.2
    elif wc < 5:
        score -= 0.3

    # Has structure
    if re.search(r"```|\n[-*•]|\n\d+\.", text):
        score += 0.15

    # No preamble
    if not re.match(r"(?i)(sure|certainly|of course|absolutely|great)[!,.]", text):
        score += 0.1

    # No truncation artifacts
    if not text.rstrip().endswith("...") and len(text) < 3000:
        score += 0.05

    return round(min(score, 1.0), 4)


def _score_recency(ts: float) -> float:
    """Score recency: newer = higher."""
    age_s = time.time() - ts
    if age_s < 60:
        return 1.0
    elif age_s < 3600:
        return 0.8
    elif age_s < 86400:
        return 0.5
    return 0.2


def rerank(items: list, query: str = "", weights: dict = None) -> list:
    """
    Rerank a list of items. Each item: {text, ts?, score?, backend?, metadata?}
    Returns sorted list with _rerank_score added.
    """
    w = weights or {"relevance": 0.4, "quality": 0.35, "recency": 0.25}
    scored = []
    for item in items:
        text = item.get("text", item.get("content", item.get("output", "")))
        ts = float(item.get("ts", time.time()))
        rel = _score_relevance(text, query) if query else 0.5
        qual = _score_quality(text)
        rec = _score_recency(ts)
        final = rel * w["relevance"] + qual * w["quality"] + rec * w["recency"]
        scored.append(
            {
                **item,
                "_rerank_score": round(final, 4),
                "_scores": {"relevance": rel, "quality": qual, "recency": rec},
            }
        )

    scored.sort(key=lambda x: x["_rerank_score"], reverse=True)
    r.hincrby(STATS_KEY, "reranked_items", len(items))
    r.hincrby(STATS_KEY, "rerank_calls", 1)
    return scored


def pick_best(items: list, query: str = "") -> dict | None:
    """Return the single best item after reranking."""
    ranked = rerank(items, query)
    return ranked[0] if ranked else None


def stats() -> dict:
    s = r.hgetall(STATS_KEY)
    return {k: int(v) for k, v in s.items()}


if __name__ == "__main__":
    query = "GPU temperature monitoring"
    candidates = [
        {
            "text": "Sure! GPU temperatures are important.",
            "ts": time.time() - 3600,
            "backend": "ol1",
        },
        {
            "text": "GPU0: 36°C, GPU1: 32°C, GPU2: 33°C. All within normal range (< 82°C).\n- Max temp: 36°C\n- VRAM usage: 15%",
            "ts": time.time(),
            "backend": "m32",
        },
        {
            "text": "Thermal monitoring tracks GPU temperature to prevent throttling.",
            "ts": time.time() - 600,
            "backend": "m2",
        },
        {
            "text": "```python\ntemp = get_gpu_temp(0)\nif temp > 82: alert()\n```\nMonitor GPU temps above 82°C.",
            "ts": time.time() - 60,
            "backend": "m2",
        },
    ]

    ranked = rerank(candidates, query)
    print(f"Reranked {len(ranked)} results for: '{query}'")
    for i, item in enumerate(ranked):
        s = item["_scores"]
        print(
            f"  #{i + 1} [{item['_rerank_score']:.3f}] rel={s['relevance']:.2f} qual={s['quality']:.2f} rec={s['recency']:.2f} | {item['text'][:60]}"
        )
    print(f"\nStats: {stats()}")
