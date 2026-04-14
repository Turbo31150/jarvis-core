#!/usr/bin/env python3
"""
jarvis_query_optimizer — LLM query optimization pipeline
Rewrites vague queries, decomposes complex ones, adds context, selects optimal params
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.query_optimizer")

LM_URL = "http://127.0.0.1:1234"
OPTIMIZER_MODEL = "qwen/qwen3.5-9b"
REDIS_PREFIX = "jarvis:qopt:"


@dataclass
class OptimizedQuery:
    original: str
    rewritten: str
    sub_queries: list[str]
    strategy: str  # simple | rewrite | decompose | augment
    params: dict  # suggested model params
    context_hints: list[str]
    latency_ms: float = 0.0
    skipped: bool = False  # True if optimization was skipped (query already good)

    def to_dict(self) -> dict:
        return {
            "original": self.original,
            "rewritten": self.rewritten,
            "sub_queries": self.sub_queries,
            "strategy": self.strategy,
            "params": self.params,
            "context_hints": self.context_hints,
            "latency_ms": round(self.latency_ms, 1),
            "skipped": self.skipped,
        }


# Heuristics to detect query quality issues
VAGUE_PATTERNS = [
    r"^(tell me|explain|describe|what is|how does)\s+\w+\s*$",
    r"^(help|assist|do)\b",
    r"^\w{1,20}\?*$",  # very short single-word queries
]

COMPLEX_PATTERNS = [
    r"\b(and also|additionally|furthermore|moreover)\b",
    r"(step\s+1|first.*then.*finally|multiple)",
    r"\?.*\?",  # multiple questions
]


class QueryOptimizer:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._cache: dict[str, OptimizedQuery] = {}

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def _classify(self, query: str) -> str:
        """Classify query to determine optimization strategy."""
        q = query.strip()
        if len(q) < 10:
            return "augment"
        if any(re.search(p, q, re.IGNORECASE) for p in VAGUE_PATTERNS):
            return "rewrite"
        if any(re.search(p, q, re.IGNORECASE) for p in COMPLEX_PATTERNS):
            return "decompose"
        if len(q.split()) > 80:
            return "rewrite"
        return "simple"

    def _suggest_params(self, query: str, strategy: str) -> dict:
        """Suggest model parameters based on query characteristics."""
        params: dict[str, Any] = {"temperature": 0.7, "max_tokens": 512}
        q_lower = query.lower()
        # Code queries: deterministic
        if any(
            kw in q_lower for kw in ["code", "function", "debug", "python", "script"]
        ):
            params["temperature"] = 0.1
            params["max_tokens"] = 1024
        # Creative: higher temperature
        elif any(kw in q_lower for kw in ["story", "poem", "creative", "write"]):
            params["temperature"] = 0.9
            params["max_tokens"] = 1024
        # Factual: low temperature
        elif any(kw in q_lower for kw in ["what is", "define", "explain", "fact"]):
            params["temperature"] = 0.3
            params["max_tokens"] = 512
        # Complex reasoning: more tokens
        if strategy == "decompose":
            params["max_tokens"] = 2048
        return params

    def _extract_context_hints(self, query: str) -> list[str]:
        """Extract domain hints to inject relevant context."""
        hints = []
        q_lower = query.lower()
        domains = {
            "gpu|vram|cuda|nvidia": "gpu_context",
            "redis|cache|memory|store": "redis_context",
            "model|llm|inference|token": "llm_context",
            "trading|btc|crypto|market": "trading_context",
            "python|code|script|function": "code_context",
            "cluster|node|m1|m2": "cluster_context",
        }
        for pattern, hint in domains.items():
            if re.search(pattern, q_lower):
                hints.append(hint)
        return hints

    async def _rewrite(self, query: str) -> str:
        """Use LLM to rewrite a vague query into a clearer one."""
        prompt = (
            f"Rewrite this query to be more specific and actionable. "
            f"Keep the same intent. Return only the rewritten query, nothing else.\n\n"
            f"Query: {query}\n\nRewritten:"
        )
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as sess:
                async with sess.post(
                    f"{LM_URL}/v1/chat/completions",
                    json={
                        "model": OPTIMIZER_MODEL,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 150,
                        "temperature": 0.3,
                    },
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            log.debug(f"Rewrite failed: {e}")
        return query

    async def _decompose(self, query: str) -> list[str]:
        """Break a complex query into sub-queries."""
        prompt = (
            f"Break this complex query into 2-4 simpler sub-questions that together answer the original. "
            f"Return as JSON array of strings.\n\nQuery: {query}\n\nSub-queries:"
        )
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as sess:
                async with sess.post(
                    f"{LM_URL}/v1/chat/completions",
                    json={
                        "model": OPTIMIZER_MODEL,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 300,
                        "temperature": 0.2,
                    },
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        raw = data["choices"][0]["message"]["content"].strip()
                        # Extract JSON array
                        m = re.search(r"\[[\s\S]*\]", raw)
                        if m:
                            return json.loads(m.group(0))
        except Exception as e:
            log.debug(f"Decompose failed: {e}")
        return [query]

    async def optimize(
        self,
        query: str,
        use_llm: bool = True,
        cache: bool = True,
    ) -> OptimizedQuery:
        t0 = time.time()
        cache_key = query[:100]

        # Cache hit
        if cache and cache_key in self._cache:
            cached = self._cache[cache_key]
            return OptimizedQuery(
                **{**cached.to_dict(), "latency_ms": 0.0, "skipped": True}
            )

        strategy = self._classify(query)
        params = self._suggest_params(query, strategy)
        context_hints = self._extract_context_hints(query)
        rewritten = query
        sub_queries = [query]

        if use_llm:
            if strategy == "rewrite" or strategy == "augment":
                rewritten = await self._rewrite(query)
            elif strategy == "decompose":
                sub_queries = await self._decompose(query)
                rewritten = sub_queries[0] if sub_queries else query

        result = OptimizedQuery(
            original=query,
            rewritten=rewritten,
            sub_queries=sub_queries,
            strategy=strategy,
            params=params,
            context_hints=context_hints,
            latency_ms=(time.time() - t0) * 1000,
        )

        if cache:
            self._cache[cache_key] = result
            if len(self._cache) > 1000:
                oldest = next(iter(self._cache))
                del self._cache[oldest]

        if self.redis:
            await self.redis.hincrby(f"{REDIS_PREFIX}stats", strategy, 1)

        log.debug(f"Query optimized: strategy={strategy} in {result.latency_ms:.0f}ms")
        return result

    async def batch_optimize(self, queries: list[str]) -> list[OptimizedQuery]:
        return await asyncio.gather(*[self.optimize(q) for q in queries])

    def stats(self) -> dict:
        by_strategy: dict[str, int] = {}
        for oq in self._cache.values():
            by_strategy[oq.strategy] = by_strategy.get(oq.strategy, 0) + 1
        return {"cached": len(self._cache), "by_strategy": by_strategy}


async def main():
    import sys

    opt = QueryOptimizer()
    await opt.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        queries = [
            "explain redis",
            "help me",
            "Write a Python async function that fetches data from an API, handles errors, retries 3 times, and also logs the results",
            "What is the GPU temperature and how do I reduce VRAM usage for the qwen model?",
            "code",
        ]
        for q in queries:
            result = await opt.optimize(q, use_llm=False)  # no LLM in demo
            print(f"\n[{result.strategy}] '{q[:50]}'")
            if result.rewritten != q:
                print(f"  → '{result.rewritten[:80]}'")
            if len(result.sub_queries) > 1:
                for i, sq in enumerate(result.sub_queries):
                    print(f"  {i + 1}. {sq[:60]}")
            print(f"  params={result.params} hints={result.context_hints}")

    elif cmd == "optimize" and len(sys.argv) > 2:
        query = " ".join(sys.argv[2:])
        result = await opt.optimize(query)
        print(json.dumps(result.to_dict(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
