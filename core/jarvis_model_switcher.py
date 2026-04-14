#!/usr/bin/env python3
"""
jarvis_model_switcher — Dynamic model switching based on task complexity and cost
Selects optimal model per request using capability matching and cost optimization
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.model_switcher")

REDIS_PREFIX = "jarvis:switcher:"


class TaskComplexity(int, Enum):
    TRIVIAL = 1  # one-word answers, classification
    SIMPLE = 2  # short Q&A, summarization
    MODERATE = 3  # code, analysis
    COMPLEX = 4  # reasoning, long-form
    EXPERT = 5  # multi-step reasoning, advanced code


class SwitchStrategy(str, Enum):
    COST_FIRST = "cost_first"  # cheapest that can handle it
    QUALITY_FIRST = "quality_first"  # best quality
    BALANCED = "balanced"  # quality/cost tradeoff
    ADAPTIVE = "adaptive"  # switch based on recent performance


@dataclass
class ModelProfile:
    name: str
    endpoint: str
    url: str
    min_complexity: TaskComplexity = TaskComplexity.TRIVIAL
    max_complexity: TaskComplexity = TaskComplexity.EXPERT
    quality_score: float = 0.7  # 0-1, subjective
    cost_per_1k_tokens: float = 0.0  # 0 = local/free
    avg_latency_ms: float = 500.0
    max_tokens: int = 8192
    tags: list[str] = field(default_factory=list)
    enabled: bool = True

    def can_handle(self, complexity: TaskComplexity) -> bool:
        return self.min_complexity <= complexity <= self.max_complexity

    def score(self, strategy: SwitchStrategy, complexity: TaskComplexity) -> float:
        if not self.can_handle(complexity):
            return -1.0
        if strategy == SwitchStrategy.COST_FIRST:
            # lower cost = higher score; use quality as tiebreaker
            return self.quality_score - self.cost_per_1k_tokens * 10
        elif strategy == SwitchStrategy.QUALITY_FIRST:
            return self.quality_score
        elif strategy == SwitchStrategy.BALANCED:
            return self.quality_score * 0.6 - self.cost_per_1k_tokens * 0.4
        else:  # ADAPTIVE
            return self.quality_score - self.cost_per_1k_tokens * 5

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "endpoint": self.endpoint,
            "min_complexity": self.min_complexity.name,
            "max_complexity": self.max_complexity.name,
            "quality_score": self.quality_score,
            "cost_per_1k": self.cost_per_1k_tokens,
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "enabled": self.enabled,
        }


@dataclass
class SwitchDecision:
    model: ModelProfile
    complexity: TaskComplexity
    strategy: SwitchStrategy
    score: float
    reason: str
    alternatives: list[str]

    def to_dict(self) -> dict:
        return {
            "selected_model": self.model.name,
            "complexity": self.complexity.name,
            "strategy": self.strategy.value,
            "score": round(self.score, 3),
            "reason": self.reason,
            "alternatives": self.alternatives,
        }


def _estimate_complexity(messages: list[dict]) -> TaskComplexity:
    """Heuristic complexity estimation from messages."""
    text = " ".join(m.get("content", "") for m in messages if m.get("role") == "user")
    words = len(text.split())
    has_code = "```" in text or "def " in text or "class " in text
    has_math = any(
        kw in text.lower() for kw in ["calculate", "solve", "prove", "derive"]
    )
    has_reason = any(
        kw in text.lower() for kw in ["why", "analyze", "compare", "explain step"]
    )
    is_short = words < 15 and not any(q in text.lower() for q in ["how", "why", "what"])

    if is_short and words < 8:
        return TaskComplexity.TRIVIAL
    if words < 30 and not has_code and not has_reason:
        return TaskComplexity.SIMPLE
    if has_code or has_math:
        return TaskComplexity.COMPLEX if has_reason else TaskComplexity.MODERATE
    if has_reason:
        return TaskComplexity.COMPLEX
    if words > 200:
        return TaskComplexity.EXPERT
    return TaskComplexity.MODERATE


class ModelSwitcher:
    def __init__(self, strategy: SwitchStrategy = SwitchStrategy.BALANCED):
        self.redis: aioredis.Redis | None = None
        self._models: list[ModelProfile] = []
        self._strategy = strategy
        self._perf: dict[str, dict] = {}  # model → {successes, failures, total_latency}
        self._stats: dict[str, int] = {
            "switched": 0,
            "no_model": 0,
            "calls": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def add_model(self, profile: ModelProfile):
        self._models.append(profile)

    def set_strategy(self, strategy: SwitchStrategy):
        self._strategy = strategy

    def select(
        self,
        messages: list[dict],
        complexity: TaskComplexity | None = None,
        tags: list[str] | None = None,
        force_model: str | None = None,
    ) -> SwitchDecision | None:
        self._stats["switched"] += 1

        if force_model:
            m = next(
                (m for m in self._models if m.name == force_model and m.enabled), None
            )
            if m:
                return SwitchDecision(
                    m,
                    complexity or TaskComplexity.MODERATE,
                    self._strategy,
                    1.0,
                    "forced",
                    [],
                )

        c = complexity or _estimate_complexity(messages)
        candidates = [m for m in self._models if m.enabled and m.can_handle(c)]

        if tags:
            tagged = [m for m in candidates if any(t in m.tags for t in tags)]
            if tagged:
                candidates = tagged

        if not candidates:
            self._stats["no_model"] += 1
            return None

        strategy = self._strategy
        # In adaptive mode, use performance data
        if strategy == SwitchStrategy.ADAPTIVE:
            for m in candidates:
                perf = self._perf.get(m.name, {})
                total = perf.get("total", 1)
                errors = perf.get("errors", 0)
                error_rate = errors / total
                # Penalize high-error models
                m_copy_score = m.quality_score * (1 - error_rate * 2)
                if m_copy_score > 0:
                    continue

        scored = sorted(candidates, key=lambda m: -m.score(strategy, c))
        best = scored[0]
        alts = [m.name for m in scored[1:3]]

        return SwitchDecision(
            model=best,
            complexity=c,
            strategy=strategy,
            score=best.score(strategy, c),
            reason=f"complexity={c.name} strategy={strategy.value}",
            alternatives=alts,
        )

    async def call(
        self,
        messages: list[dict],
        complexity: TaskComplexity | None = None,
        params: dict | None = None,
    ) -> dict[str, Any]:
        self._stats["calls"] += 1
        decision = self.select(messages, complexity)
        if not decision:
            raise RuntimeError("No model available for this request")

        model = decision.model
        t0 = time.time()
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60)
            ) as sess:
                payload = {"model": model.name, "messages": messages, **(params or {})}
                async with sess.post(
                    f"{model.url}/v1/chat/completions", json=payload
                ) as r:
                    lat = (time.time() - t0) * 1000
                    if r.status == 200:
                        data = await r.json(content_type=None)
                        content = data["choices"][0]["message"]["content"]
                        # Update perf
                        p = self._perf.setdefault(
                            model.name, {"total": 0, "errors": 0, "latency_sum": 0.0}
                        )
                        p["total"] += 1
                        p["latency_sum"] += lat
                        # EMA latency
                        model.avg_latency_ms = 0.2 * lat + 0.8 * model.avg_latency_ms
                        return {
                            "content": content,
                            "model": model.name,
                            "latency_ms": lat,
                            "complexity": decision.complexity.name,
                        }
                    else:
                        raise RuntimeError(f"HTTP {r.status}")
        except Exception:
            p = self._perf.setdefault(
                model.name, {"total": 0, "errors": 0, "latency_sum": 0.0}
            )
            p["total"] += 1
            p["errors"] += 1
            raise

    def list_models(self) -> list[dict]:
        return [m.to_dict() for m in self._models]

    def stats(self) -> dict:
        return {
            **self._stats,
            "models": len(self._models),
            "strategy": self._strategy.value,
        }


def build_jarvis_switcher() -> ModelSwitcher:
    switcher = ModelSwitcher(strategy=SwitchStrategy.BALANCED)
    switcher.add_model(
        ModelProfile(
            "gemma3-4b",
            "http://127.0.0.1:11434",
            "http://127.0.0.1:11434",
            min_complexity=TaskComplexity.TRIVIAL,
            max_complexity=TaskComplexity.SIMPLE,
            quality_score=0.55,
            cost_per_1k_tokens=0.0,
            avg_latency_ms=400,
            tags=["fast", "local"],
        )
    )
    switcher.add_model(
        ModelProfile(
            "qwen3.5-9b",
            "http://192.168.1.85:1234",
            "http://192.168.1.85:1234",
            min_complexity=TaskComplexity.SIMPLE,
            max_complexity=TaskComplexity.MODERATE,
            quality_score=0.72,
            cost_per_1k_tokens=0.0,
            avg_latency_ms=600,
            tags=["fast"],
        )
    )
    switcher.add_model(
        ModelProfile(
            "qwen3.5-27b",
            "http://192.168.1.85:1234",
            "http://192.168.1.85:1234",
            min_complexity=TaskComplexity.MODERATE,
            max_complexity=TaskComplexity.EXPERT,
            quality_score=0.88,
            cost_per_1k_tokens=0.0,
            avg_latency_ms=1200,
            tags=["quality"],
        )
    )
    switcher.add_model(
        ModelProfile(
            "deepseek-r1",
            "http://192.168.1.26:1234",
            "http://192.168.1.26:1234",
            min_complexity=TaskComplexity.COMPLEX,
            max_complexity=TaskComplexity.EXPERT,
            quality_score=0.92,
            cost_per_1k_tokens=0.0,
            avg_latency_ms=2000,
            tags=["reasoning"],
        )
    )
    return switcher


async def main():
    import sys

    switcher = build_jarvis_switcher()
    await switcher.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        test_cases = [
            ([{"role": "user", "content": "Hi"}], "trivial"),
            (
                [
                    {
                        "role": "user",
                        "content": "Summarize the French Revolution in 3 sentences.",
                    }
                ],
                "simple",
            ),
            (
                [
                    {
                        "role": "user",
                        "content": "Write a Python class for a binary search tree with insert and search.",
                    }
                ],
                "code",
            ),
            (
                [
                    {
                        "role": "user",
                        "content": "Analyze why quantum computing threatens RSA encryption and explain the mathematical underpinning.",
                    }
                ],
                "complex",
            ),
        ]
        print("Model selection:")
        for messages, label in test_cases:
            d = switcher.select(messages)
            if d:
                print(
                    f"  [{label:<10}] complexity={d.complexity.name:<10} → {d.model.name}"
                )

        print(f"\nModels:\n{json.dumps(switcher.list_models(), indent=2)[:400]}")
        print(f"\nStats: {json.dumps(switcher.stats(), indent=2)}")

    elif cmd == "list":
        for m in switcher.list_models():
            print(
                f"  {m['name']:<25} quality={m['quality_score']} complexity={m['min_complexity']}→{m['max_complexity']}"
            )

    elif cmd == "stats":
        print(json.dumps(switcher.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
