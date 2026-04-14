#!/usr/bin/env python3
"""
jarvis_query_router — Intent-based query routing to specialized handlers
Routes queries to the best handler based on intent classification and scoring
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.query_router")

REDIS_PREFIX = "jarvis:qrouter:"


@dataclass
class RouteRule:
    name: str
    patterns: list[str]  # regex patterns
    keywords: list[str]  # keyword triggers
    handler_key: str
    priority: int = 5  # 1-10
    min_score: float = 0.3
    tags: list[str] = field(default_factory=list)

    def score(self, query: str) -> float:
        q = query.lower()
        total = 0.0
        matches = 0

        for pat in self.patterns:
            if re.search(pat, q, re.IGNORECASE):
                total += 0.4
                matches += 1

        for kw in self.keywords:
            if kw.lower() in q:
                total += 0.2
                matches += 1

        if not self.patterns and not self.keywords:
            return 0.0

        # Normalize: cap at 1.0, boost for multiple matches
        base = min(total, 1.0)
        bonus = 0.1 * min(matches - 1, 3) if matches > 1 else 0.0
        return min(base + bonus, 1.0)


@dataclass
class RouteDecision:
    query: str
    handler_key: str
    rule_name: str
    score: float
    alternatives: list[dict]
    duration_ms: float
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "handler_key": self.handler_key,
            "rule_name": self.rule_name,
            "score": round(self.score, 3),
            "alternatives": self.alternatives[:3],
            "duration_ms": round(self.duration_ms, 1),
        }


class QueryRouter:
    def __init__(self, fallback_handler: str = "default"):
        self.redis: aioredis.Redis | None = None
        self._rules: list[RouteRule] = []
        self._handlers: dict[str, Callable] = {}
        self._fallback = fallback_handler
        self._history: list[RouteDecision] = []
        self._stats: dict[str, int] = {"routed": 0, "fallbacks": 0, "executed": 0}

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def add_rule(self, rule: RouteRule) -> "QueryRouter":
        self._rules.append(rule)
        # Keep sorted by priority desc
        self._rules.sort(key=lambda r: -r.priority)
        return self

    def register_handler(self, key: str, fn: Callable):
        self._handlers[key] = fn

    def classify(self, query: str) -> RouteDecision:
        t0 = time.time()
        scores = []

        for rule in self._rules:
            s = rule.score(query)
            if s >= rule.min_score:
                scores.append((s, rule))

        scores.sort(key=lambda x: (-x[0], -x[1].priority))

        if scores:
            best_score, best_rule = scores[0]
            handler_key = best_rule.handler_key
            rule_name = best_rule.name
        else:
            best_score = 0.0
            handler_key = self._fallback
            rule_name = "fallback"
            self._stats["fallbacks"] += 1

        alts = [
            {"rule": r.name, "handler": r.handler_key, "score": round(s, 3)}
            for s, r in scores[1:4]
        ]

        decision = RouteDecision(
            query=query[:200],
            handler_key=handler_key,
            rule_name=rule_name,
            score=best_score,
            alternatives=alts,
            duration_ms=(time.time() - t0) * 1000,
        )
        self._stats["routed"] += 1
        self._history.append(decision)
        return decision

    async def route(self, query: str, context: dict | None = None) -> Any:
        decision = self.classify(query)
        handler = self._handlers.get(decision.handler_key)

        if not handler:
            handler = self._handlers.get(self._fallback)
        if not handler:
            return {
                "error": f"No handler for '{decision.handler_key}'",
                "decision": decision.to_dict(),
            }

        self._stats["executed"] += 1
        ctx = context or {}
        try:
            if asyncio.iscoroutinefunction(handler):
                result = await handler(query, decision, ctx)
            else:
                result = handler(query, decision, ctx)
            return result
        except Exception as e:
            log.error(f"Handler '{decision.handler_key}' error: {e}")
            return {"error": str(e), "decision": decision.to_dict()}

    def explain(self, query: str) -> list[dict]:
        """Return all rule scores for debugging."""
        return sorted(
            [
                {
                    "rule": r.name,
                    "handler": r.handler_key,
                    "score": round(r.score(query), 3),
                }
                for r in self._rules
            ],
            key=lambda x: -x["score"],
        )

    def stats(self) -> dict:
        handler_counts: dict[str, int] = {}
        for d in self._history:
            handler_counts[d.handler_key] = handler_counts.get(d.handler_key, 0) + 1
        return {
            **self._stats,
            "rules": len(self._rules),
            "handlers": len(self._handlers),
            "handler_distribution": handler_counts,
            "fallback_rate": round(
                self._stats["fallbacks"] / max(self._stats["routed"], 1) * 100, 1
            ),
        }


def build_jarvis_router() -> QueryRouter:
    """Pre-built router with JARVIS-specific rules."""
    router = QueryRouter(fallback_handler="llm_general")
    rules = [
        RouteRule(
            "cluster_health",
            [r"cluster|node|gpu|vram|temperature"],
            ["m1", "m2", "ol1", "gpu", "thermal"],
            "cluster_handler",
            priority=9,
        ),
        RouteRule(
            "trading",
            [r"trade|crypto|bitcoin|price|market|btc|eth"],
            ["trading", "mexc", "hyperliquid", "position"],
            "trading_handler",
            priority=9,
        ),
        RouteRule(
            "code_gen",
            [r"write|implement|create.*function|def |class "],
            ["python", "typescript", "code", "function", "class"],
            "code_handler",
            priority=8,
        ),
        RouteRule(
            "memory_search",
            [r"remember|recall|what did|find.*memory|search"],
            ["memory", "recall", "remember", "history"],
            "memory_handler",
            priority=7,
        ),
        RouteRule(
            "system_ops",
            [r"kill|restart|deploy|start|stop.*service"],
            ["systemd", "service", "process", "deploy"],
            "sysops_handler",
            priority=8,
        ),
        RouteRule(
            "web_search",
            [r"search.*web|look up|find.*online|latest news"],
            ["search", "google", "web", "online"],
            "web_handler",
            priority=6,
        ),
        RouteRule(
            "math",
            [r"\d+[\+\-\*/]\d+|calculate|compute|solve"],
            ["math", "calculate", "equation", "formula"],
            "math_handler",
            priority=7,
        ),
    ]
    for rule in rules:
        router.add_rule(rule)
    return router


async def main():
    import sys

    router = build_jarvis_router()
    await router.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        queries = [
            "What's the GPU temperature on M1?",
            "Write a Python function to sort a list",
            "What's the Bitcoin price right now?",
            "Restart the nginx service",
            "Calculate 2 + 2 * 10",
            "Tell me a joke",
        ]
        for q in queries:
            decision = router.classify(q)
            print(f"  [{decision.handler_key:<18}] ({decision.score:.2f}) {q}")

        print(f"\nStats: {json.dumps(router.stats(), indent=2)}")

    elif cmd == "explain" and len(sys.argv) > 2:
        query = " ".join(sys.argv[2:])
        scores = router.explain(query)
        for s in scores[:8]:
            print(f"  {s['rule']:<20} → {s['handler']:<18} score={s['score']:.3f}")

    elif cmd == "stats":
        print(json.dumps(router.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
