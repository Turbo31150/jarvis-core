#!/usr/bin/env python3
"""
jarvis_multimodel_router — Route requests across models with capability matching
Selects optimal model based on task type, context length, language, and performance history
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.multimodel_router")

REDIS_PREFIX = "jarvis:mmrouter:"


class Capability(str, Enum):
    CHAT = "chat"
    CODE = "code"
    REASONING = "reasoning"
    EMBEDDING = "embedding"
    VISION = "vision"
    FUNCTION_CALL = "function_call"
    LONG_CONTEXT = "long_context"  # >16k tokens
    MULTILINGUAL = "multilingual"


class RoutingStrategy(str, Enum):
    CAPABILITY = "capability"  # match required capabilities
    PERFORMANCE = "performance"  # historical best P95 latency
    COST = "cost"  # prefer cheapest
    QUALITY = "quality"  # prefer highest quality score
    RANDOM = "random"


@dataclass
class ModelSpec:
    model_id: str
    name: str
    endpoint: str
    capabilities: set[Capability]
    context_window: int = 8192
    quality: float = 0.75  # 0-1
    cost_per_1k: float = 0.0  # 0 = local
    max_concurrency: int = 4
    enabled: bool = True
    # Runtime
    _active: int = 0
    _p95_ms: float = 0.0
    _call_count: int = 0
    _error_count: int = 0

    @property
    def available(self) -> bool:
        return self.enabled and self._active < self.max_concurrency

    @property
    def error_rate(self) -> float:
        return self._error_count / max(self._call_count, 1)

    def supports(self, caps: set[Capability]) -> bool:
        return caps.issubset(self.capabilities)

    def to_dict(self) -> dict:
        return {
            "model_id": self.model_id,
            "name": self.name,
            "capabilities": [c.value for c in self.capabilities],
            "context_window": self.context_window,
            "quality": self.quality,
            "active": self._active,
            "p95_ms": round(self._p95_ms, 1),
            "error_rate": round(self.error_rate, 3),
            "enabled": self.enabled,
        }


@dataclass
class RouteDecision:
    model: ModelSpec
    strategy: RoutingStrategy
    required_caps: set[Capability]
    score: float
    alternatives: list[str]
    reason: str

    def to_dict(self) -> dict:
        return {
            "model": self.model.model_id,
            "strategy": self.strategy.value,
            "score": round(self.score, 3),
            "reason": self.reason,
            "alternatives": self.alternatives,
        }


def _infer_capabilities(
    messages: list[dict], tools: list | None = None
) -> set[Capability]:
    caps = {Capability.CHAT}
    text = " ".join(
        m.get("content", "") for m in messages if m.get("role") == "user"
    ).lower()

    if tools:
        caps.add(Capability.FUNCTION_CALL)
    if any(
        kw in text
        for kw in ["code", "function", "class", "debug", "python", "javascript", "```"]
    ):
        caps.add(Capability.CODE)
    if any(
        kw in text
        for kw in ["why", "reason", "logic", "prove", "analyze", "step by step"]
    ):
        caps.add(Capability.REASONING)
    if sum(len(m.get("content", "")) for m in messages) > 12000:
        caps.add(Capability.LONG_CONTEXT)

    return caps


class MultimodelRouter:
    def __init__(self, strategy: RoutingStrategy = RoutingStrategy.CAPABILITY):
        self.redis: aioredis.Redis | None = None
        self._models: list[ModelSpec] = []
        self._strategy = strategy
        self._latency_windows: dict[str, list[float]] = {}
        self._stats: dict[str, int] = {
            "routed": 0,
            "calls": 0,
            "errors": 0,
            "no_match": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def add_model(self, spec: ModelSpec):
        self._models.append(spec)
        self._latency_windows[spec.model_id] = []

    def _update_p95(self, model_id: str, latency_ms: float):
        w = self._latency_windows.setdefault(model_id, [])
        w.append(latency_ms)
        if len(w) > 200:
            w.pop(0)
        if w:
            s = sorted(w)
            idx = min(int(len(s) * 0.95), len(s) - 1)
            model = next((m for m in self._models if m.model_id == model_id), None)
            if model:
                model._p95_ms = s[idx]

    def route(
        self,
        messages: list[dict],
        required_caps: set[Capability] | None = None,
        tools: list | None = None,
        strategy: RoutingStrategy | None = None,
        exclude: list[str] | None = None,
    ) -> RouteDecision | None:
        self._stats["routed"] += 1
        strat = strategy or self._strategy
        caps = required_caps or _infer_capabilities(messages, tools)
        excl = set(exclude or [])

        candidates = [
            m
            for m in self._models
            if m.available
            and m.model_id not in excl
            and m.supports(caps)
            and m.error_rate < 0.5
        ]

        if not candidates:
            self._stats["no_match"] += 1
            return None

        if strat == RoutingStrategy.RANDOM:
            import random

            best = random.choice(candidates)
        elif strat == RoutingStrategy.PERFORMANCE:
            best = min(
                candidates, key=lambda m: m._p95_ms if m._p95_ms > 0 else float("inf")
            )
        elif strat == RoutingStrategy.COST:
            best = min(candidates, key=lambda m: m.cost_per_1k)
        elif strat == RoutingStrategy.QUALITY:
            best = max(candidates, key=lambda m: m.quality)
        else:  # CAPABILITY / default
            # Prefer models with exact capability match (not over-provisioned)
            def cap_score(m: ModelSpec) -> float:
                excess = len(m.capabilities) - len(caps)
                return m.quality - excess * 0.02 - m.cost_per_1k * 0.1

            best = max(candidates, key=cap_score)

        alts = [m.model_id for m in candidates if m is not best][:3]

        return RouteDecision(
            model=best,
            strategy=strat,
            required_caps=caps,
            score=best.quality,
            alternatives=alts,
            reason=f"caps={[c.value for c in caps]} strategy={strat.value}",
        )

    async def call(
        self,
        messages: list[dict],
        required_caps: set[Capability] | None = None,
        tools: list | None = None,
        params: dict | None = None,
        fallback: bool = True,
    ) -> dict[str, Any]:
        self._stats["calls"] += 1
        tried: list[str] = []

        while True:
            decision = self.route(messages, required_caps, tools, exclude=tried)
            if not decision:
                self._stats["errors"] += 1
                raise RuntimeError(f"No model available (tried: {tried})")

            model = decision.model
            model._active += 1
            model._call_count += 1
            t0 = time.time()
            tried.append(model.model_id)

            try:
                payload = {
                    "model": model.model_id,
                    "messages": messages,
                    **(params or {}),
                }
                if tools:
                    payload["tools"] = tools
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=60)
                ) as sess:
                    async with sess.post(
                        f"{model.endpoint}/v1/chat/completions", json=payload
                    ) as r:
                        lat = (time.time() - t0) * 1000
                        self._update_p95(model.model_id, lat)
                        if r.status == 200:
                            data = await r.json(content_type=None)
                            return {
                                "content": data["choices"][0]["message"]["content"],
                                "model": model.model_id,
                                "latency_ms": lat,
                                "capabilities_used": [
                                    c.value for c in decision.required_caps
                                ],
                            }
                        else:
                            raise RuntimeError(f"HTTP {r.status}")
            except Exception as e:
                model._error_count += 1
                if not fallback:
                    self._stats["errors"] += 1
                    raise
                log.debug(f"Model {model.model_id} failed: {e}, trying next")
            finally:
                model._active -= 1

    def list_models(self) -> list[dict]:
        return [m.to_dict() for m in self._models]

    def stats(self) -> dict:
        return {
            **self._stats,
            "models": len(self._models),
            "strategy": self._strategy.value,
        }


def build_jarvis_multimodel_router() -> MultimodelRouter:
    router = MultimodelRouter(RoutingStrategy.CAPABILITY)
    router.add_model(
        ModelSpec(
            "gemma3-4b",
            "gemma3:4b",
            "http://127.0.0.1:11434",
            {Capability.CHAT},
            context_window=8192,
            quality=0.55,
            max_concurrency=2,
        )
    )
    router.add_model(
        ModelSpec(
            "qwen3.5-9b",
            "qwen3.5-9b",
            "http://192.168.1.85:1234",
            {Capability.CHAT, Capability.CODE, Capability.MULTILINGUAL},
            context_window=32768,
            quality=0.72,
            max_concurrency=4,
        )
    )
    router.add_model(
        ModelSpec(
            "qwen3.5-27b",
            "qwen3.5-27b",
            "http://192.168.1.85:1234",
            {
                Capability.CHAT,
                Capability.CODE,
                Capability.REASONING,
                Capability.FUNCTION_CALL,
                Capability.LONG_CONTEXT,
            },
            context_window=131072,
            quality=0.88,
            max_concurrency=2,
        )
    )
    router.add_model(
        ModelSpec(
            "deepseek-r1",
            "deepseek-r1-0528",
            "http://192.168.1.26:1234",
            {Capability.CHAT, Capability.REASONING, Capability.CODE},
            context_window=65536,
            quality=0.92,
            max_concurrency=2,
        )
    )
    router.add_model(
        ModelSpec(
            "nomic-embed",
            "nomic-embed-text",
            "http://192.168.1.26:1234",
            {Capability.EMBEDDING},
            context_window=8192,
            quality=0.85,
            max_concurrency=8,
        )
    )
    return router


async def main():
    import sys

    router = build_jarvis_multimodel_router()
    await router.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        test_cases = [
            ([{"role": "user", "content": "Hello!"}], None, "simple chat"),
            (
                [{"role": "user", "content": "Write a Python class for BFS."}],
                {Capability.CODE},
                "code",
            ),
            (
                [{"role": "user", "content": "Reason step by step: is P=NP?"}],
                {Capability.REASONING},
                "reasoning",
            ),
        ]
        print("Route decisions:")
        for messages, caps, label in test_cases:
            d = router.route(messages, caps)
            if d:
                print(
                    f"  [{label:<15}] → {d.model.model_id:<20} caps={[c.value for c in d.required_caps]}"
                )

        print("\nModels:\n")
        for m in router.list_models():
            print(
                f"  {m['model_id']:<20} quality={m['quality']} caps={m['capabilities']}"
            )

        print(f"\nStats: {json.dumps(router.stats(), indent=2)}")

    elif cmd == "list":
        for m in router.list_models():
            print(f"  {m['model_id']:<25} {m['capabilities']}")

    elif cmd == "stats":
        print(json.dumps(router.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
