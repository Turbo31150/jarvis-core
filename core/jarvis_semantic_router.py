#!/usr/bin/env python3
"""
jarvis_semantic_router — Embedding-based semantic routing for LLM requests
Routes queries to specialized handlers using cosine similarity against route embeddings
"""

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Callable

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.semantic_router")

REDIS_PREFIX = "jarvis:semrouter:"
EMBED_URL = "http://192.168.1.26:1234"
EMBED_MODEL = "nomic-embed-text"


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    ma = math.sqrt(sum(x * x for x in a))
    mb = math.sqrt(sum(x * x for x in b))
    return dot / max(ma * mb, 1e-9)


def _keyword_embed(text: str) -> list[float]:
    import hashlib

    words = text.lower().split()
    dim = 256
    vec = [0.0] * dim
    for word in set(words):
        h = int(hashlib.md5(word.encode()).hexdigest()[:8], 16)
        vec[h % dim] += words.count(word) / max(len(words), 1)
    mag = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / mag for x in vec]


@dataclass
class SemanticRoute:
    name: str
    description: str
    utterances: list[str]  # example phrases that map to this route
    handler: Callable | None = None
    metadata: dict = field(default_factory=dict)
    threshold: float = 0.5
    priority: int = 5
    _embeddings: list[list[float]] = field(default_factory=list)

    def centroid(self) -> list[float]:
        if not self._embeddings:
            return []
        dim = len(self._embeddings[0])
        c = [
            sum(e[i] for e in self._embeddings) / len(self._embeddings)
            for i in range(dim)
        ]
        mag = math.sqrt(sum(x * x for x in c)) or 1.0
        return [x / mag for x in c]

    def score(self, query_emb: list[float]) -> float:
        if not self._embeddings:
            return 0.0
        scores = [_cosine(query_emb, emb) for emb in self._embeddings]
        return max(scores)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "utterance_count": len(self.utterances),
            "embedded": len(self._embeddings),
            "threshold": self.threshold,
            "priority": self.priority,
        }


@dataclass
class RouteResult:
    query: str
    matched_route: str | None
    score: float
    all_scores: dict[str, float]
    duration_ms: float
    fallback: bool = False

    def to_dict(self) -> dict:
        return {
            "query": self.query[:100],
            "matched_route": self.matched_route,
            "score": round(self.score, 4),
            "top_routes": sorted(
                [(k, round(v, 4)) for k, v in self.all_scores.items()],
                key=lambda x: -x[1],
            )[:3],
            "duration_ms": round(self.duration_ms, 1),
            "fallback": self.fallback,
        }


class SemanticRouter:
    def __init__(
        self,
        embed_url: str = EMBED_URL,
        embed_model: str = EMBED_MODEL,
        fallback_route: str | None = None,
    ):
        self.redis: aioredis.Redis | None = None
        self._routes: dict[str, SemanticRoute] = {}
        self._embed_url = embed_url
        self._embed_model = embed_model
        self._fallback_route = fallback_route
        self._embed_cache: dict[str, list[float]] = {}
        self._stats: dict[str, int] = {
            "routed": 0,
            "fallbacks": 0,
            "embed_calls": 0,
            "cache_hits": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    async def _embed(self, text: str) -> list[float]:
        key = text[:200]
        if key in self._embed_cache:
            self._stats["cache_hits"] += 1
            return self._embed_cache[key]

        self._stats["embed_calls"] += 1
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=8)
            ) as sess:
                async with sess.post(
                    f"{self._embed_url}/v1/embeddings",
                    json={"model": self._embed_model, "input": text[:2048]},
                ) as r:
                    if r.status == 200:
                        data = await r.json(content_type=None)
                        emb = data["data"][0]["embedding"]
                        mag = math.sqrt(sum(x * x for x in emb))
                        if mag > 1e-9:
                            emb = [x / mag for x in emb]
                        self._embed_cache[key] = emb
                        return emb
        except Exception as e:
            log.debug(f"Embed error: {e}")

        emb = _keyword_embed(text)
        self._embed_cache[key] = emb
        return emb

    async def add_route(self, route: SemanticRoute):
        for utterance in route.utterances:
            emb = await self._embed(utterance)
            route._embeddings.append(emb)
        self._routes[route.name] = route
        log.info(f"Route added: {route.name} ({len(route.utterances)} utterances)")

    def remove_route(self, name: str):
        self._routes.pop(name, None)

    async def route(self, query: str) -> RouteResult:
        t0 = time.time()
        self._stats["routed"] += 1
        query_emb = await self._embed(query)

        scores: dict[str, float] = {}
        for name, route in self._routes.items():
            scores[name] = route.score(query_emb)

        # Find best above threshold
        candidates = [
            (name, score)
            for name, score in scores.items()
            if score >= self._routes[name].threshold
        ]
        candidates.sort(key=lambda x: (-x[1], -self._routes[x[0]].priority))

        matched = None
        matched_score = 0.0
        fallback = False

        if candidates:
            matched, matched_score = candidates[0]
        elif self._fallback_route:
            matched = self._fallback_route
            matched_score = 0.0
            fallback = True
            self._stats["fallbacks"] += 1

        # Execute handler if present
        if matched and matched in self._routes:
            route = self._routes[matched]
            if route.handler and not fallback:
                try:
                    if asyncio.iscoroutinefunction(route.handler):
                        await route.handler(query, matched_score)
                    else:
                        route.handler(query, matched_score)
                except Exception as e:
                    log.warning(f"Route handler error: {e}")

        return RouteResult(
            query=query,
            matched_route=matched,
            score=matched_score,
            all_scores=scores,
            duration_ms=(time.time() - t0) * 1000,
            fallback=fallback,
        )

    def list_routes(self) -> list[dict]:
        return [r.to_dict() for r in self._routes.values()]

    def stats(self) -> dict:
        return {**self._stats, "routes": len(self._routes)}


async def build_jarvis_router() -> SemanticRouter:
    router = SemanticRouter(fallback_route="general")
    await router.add_route(
        SemanticRoute(
            name="cluster_ops",
            description="Cluster and infrastructure operations",
            utterances=[
                "check GPU temperature",
                "node health status",
                "VRAM usage",
                "cluster nodes",
                "M1 is down",
            ],
            threshold=0.45,
            priority=8,
        )
    )
    await router.add_route(
        SemanticRoute(
            name="trading",
            description="Trading and market analysis",
            utterances=[
                "BTC price",
                "crypto market",
                "trading signal",
                "buy ETH",
                "portfolio performance",
            ],
            threshold=0.45,
            priority=7,
        )
    )
    await router.add_route(
        SemanticRoute(
            name="code",
            description="Code generation and review",
            utterances=[
                "write a function",
                "debug this code",
                "refactor",
                "implement feature",
                "fix the bug",
            ],
            threshold=0.45,
            priority=7,
        )
    )
    await router.add_route(
        SemanticRoute(
            name="general",
            description="General purpose fallback",
            utterances=["help me", "what can you do", "explain", "tell me about"],
            threshold=0.3,
            priority=1,
        )
    )
    return router


async def main():
    import sys

    router = await build_jarvis_router()
    await router.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        queries = [
            "What's the GPU temperature on M1?",
            "Should I buy Bitcoin now?",
            "Write a Python function to sort a list",
            "Hello, how are you?",
            "Check cluster node health",
        ]
        for q in queries:
            result = await router.route(q)
            icon = "✅" if not result.fallback else "🔄"
            print(f"{icon} [{result.matched_route:<15}] {result.score:.3f} — {q[:50]}")

        print(f"\nStats: {json.dumps(router.stats(), indent=2)}")

    elif cmd == "list":
        for r in router.list_routes():
            print(
                f"  {r['name']:<20} {r['utterance_count']} utterances threshold={r['threshold']}"
            )

    elif cmd == "stats":
        print(json.dumps(router.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
