#!/usr/bin/env python3
"""
jarvis_consensus_v2 — Multi-model consensus with weighted voting and divergence detection
Queries multiple LLMs in parallel, computes agreement, returns best answer
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.consensus_v2")

REDIS_PREFIX = "jarvis:cons2:"

NODES = {
    "M1": "http://127.0.0.1:1234",
    "M2": "http://192.168.1.26:1234",
}

DEFAULT_MODELS = [
    {"node": "M1", "model": "qwen/qwen3.5-9b", "weight": 1.0},
    {"node": "M1", "model": "qwen/qwen3.5-27b-claude", "weight": 1.5},
    {"node": "M2", "model": "qwen/qwen3.5-35b-a3b", "weight": 2.0},
]


@dataclass
class ModelResponse:
    model: str
    node: str
    weight: float
    content: str
    latency_ms: float
    tokens: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.content)

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "node": self.node,
            "weight": self.weight,
            "content": self.content[:200],
            "latency_ms": round(self.latency_ms, 1),
            "error": self.error,
        }


@dataclass
class ConsensusResult:
    prompt: str
    responses: list[ModelResponse]
    winner: ModelResponse | None
    agreement_score: float  # 0-1: how similar the responses are
    strategy: str
    total_ms: float
    divergent: bool = False  # True if models strongly disagree

    def to_dict(self) -> dict:
        return {
            "winner": self.winner.to_dict() if self.winner else None,
            "agreement_score": round(self.agreement_score, 3),
            "strategy": self.strategy,
            "total_ms": round(self.total_ms, 1),
            "divergent": self.divergent,
            "responses": len(self.responses),
            "ok_responses": sum(1 for r in self.responses if r.ok),
        }


def _jaccard(a: str, b: str) -> float:
    wa = set(re.findall(r"\w+", a.lower()))
    wb = set(re.findall(r"\w+", b.lower()))
    if not wa and not wb:
        return 1.0
    return len(wa & wb) / max(len(wa | wb), 1)


def _pairwise_agreement(responses: list[ModelResponse]) -> float:
    ok = [r for r in responses if r.ok]
    if len(ok) < 2:
        return 1.0
    scores = []
    for i in range(len(ok)):
        for j in range(i + 1, len(ok)):
            scores.append(_jaccard(ok[i].content, ok[j].content))
    return sum(scores) / len(scores)


class ConsensusEngineV2:
    def __init__(self, divergence_threshold: float = 0.3):
        self.redis: aioredis.Redis | None = None
        self.divergence_threshold = divergence_threshold
        self._stats: dict[str, int] = {
            "queries": 0,
            "consensus": 0,
            "divergent": 0,
            "single": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    async def _query_model(
        self,
        node: str,
        model: str,
        weight: float,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
    ) -> ModelResponse:
        url = NODES.get(node, NODES["M1"])
        t0 = time.time()
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=45)
            ) as sess:
                async with sess.post(
                    f"{url}/v1/chat/completions",
                    json={
                        "model": model,
                        "messages": messages,
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                    },
                ) as r:
                    latency_ms = (time.time() - t0) * 1000
                    if r.status == 200:
                        data = await r.json()
                        content = data["choices"][0]["message"]["content"].strip()
                        tokens = data.get("usage", {}).get(
                            "completion_tokens", len(content.split())
                        )
                        return ModelResponse(
                            model=model,
                            node=node,
                            weight=weight,
                            content=content,
                            latency_ms=latency_ms,
                            tokens=tokens,
                        )
                    else:
                        return ModelResponse(
                            model=model,
                            node=node,
                            weight=weight,
                            content="",
                            latency_ms=latency_ms,
                            error=f"HTTP {r.status}",
                        )
        except Exception as e:
            return ModelResponse(
                model=model,
                node=node,
                weight=weight,
                content="",
                latency_ms=(time.time() - t0) * 1000,
                error=str(e)[:100],
            )

    def _pick_winner(
        self, responses: list[ModelResponse], strategy: str
    ) -> ModelResponse | None:
        ok = [r for r in responses if r.ok]
        if not ok:
            return None

        if strategy == "fastest":
            return min(ok, key=lambda r: r.latency_ms)

        if strategy == "heaviest":
            return max(ok, key=lambda r: r.weight)

        if strategy == "majority":
            # Find response most similar to others (centroid)
            scores = {}
            for r in ok:
                sim = sum(
                    _jaccard(r.content, other.content) * other.weight
                    for other in ok
                    if other is not r
                )
                scores[r.model] = sim
            best_model = max(scores, key=scores.get)
            return next(r for r in ok if r.model == best_model)

        if strategy == "weighted_majority":
            # Weight by model weight × similarity to others
            scores = {}
            for r in ok:
                sim = sum(
                    _jaccard(r.content, other.content) for other in ok if other is not r
                )
                scores[r.model] = sim * r.weight
            best_model = max(scores, key=scores.get)
            return next(r for r in ok if r.model == best_model)

        # Default: highest weight
        return max(ok, key=lambda r: r.weight)

    async def query(
        self,
        prompt: str,
        system: str = "",
        models: list[dict] | None = None,
        strategy: str = "weighted_majority",
        max_tokens: int = 512,
        temperature: float = 0.7,
        timeout_s: float = 40.0,
    ) -> ConsensusResult:
        t0 = time.time()
        model_list = models or DEFAULT_MODELS
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        # Query all models in parallel with timeout
        tasks = [
            self._query_model(
                m["node"],
                m["model"],
                m.get("weight", 1.0),
                messages,
                max_tokens,
                temperature,
            )
            for m in model_list
        ]
        try:
            responses = await asyncio.wait_for(
                asyncio.gather(*tasks), timeout=timeout_s
            )
        except asyncio.TimeoutError:
            responses = []

        ok_responses = [r for r in responses if r.ok]
        agreement = _pairwise_agreement(list(responses))
        divergent = agreement < self.divergence_threshold and len(ok_responses) >= 2

        if len(ok_responses) <= 1:
            effective_strategy = "single"
            self._stats["single"] += 1
        else:
            effective_strategy = strategy
            if divergent:
                self._stats["divergent"] += 1
            else:
                self._stats["consensus"] += 1

        winner = self._pick_winner(list(responses), effective_strategy)
        total_ms = (time.time() - t0) * 1000
        self._stats["queries"] += 1

        result = ConsensusResult(
            prompt=prompt[:100],
            responses=list(responses),
            winner=winner,
            agreement_score=agreement,
            strategy=effective_strategy,
            total_ms=total_ms,
            divergent=divergent,
        )

        if divergent:
            log.warning(
                f"Consensus divergence: agreement={agreement:.2f} across {len(ok_responses)} models"
            )

        if self.redis:
            await self.redis.hincrby(f"{REDIS_PREFIX}stats", "queries", 1)
            await self.redis.hincrby(
                f"{REDIS_PREFIX}stats", "divergent" if divergent else "consensus", 1
            )

        return result

    async def quick(self, prompt: str, system: str = "") -> str:
        """Query fastest available model — no consensus, just speed."""
        result = await self.query(
            prompt,
            system=system,
            models=[DEFAULT_MODELS[0]],
            strategy="fastest",
            max_tokens=256,
        )
        return result.winner.content if result.winner else ""

    def stats(self) -> dict:
        return {
            **self._stats,
            "consensus_rate": round(
                self._stats["consensus"] / max(self._stats["queries"], 1), 3
            ),
        }


async def main():
    import sys

    engine = ConsensusEngineV2()
    await engine.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        # Simulate with fewer models (only M1 likely running)
        models = [
            {"node": "M1", "model": "qwen/qwen3.5-9b", "weight": 1.0},
        ]
        result = await engine.query(
            "What is 2 + 2?",
            models=models,
            max_tokens=50,
        )
        if result.winner:
            print(f"Winner [{result.winner.model}]: {result.winner.content[:100]}")
        print(
            f"Agreement: {result.agreement_score:.2f} | Strategy: {result.strategy} | {result.total_ms:.0f}ms"
        )
        print(f"Stats: {json.dumps(engine.stats(), indent=2)}")

    elif cmd == "query" and len(sys.argv) > 2:
        prompt = " ".join(sys.argv[2:])
        result = await engine.query(prompt)
        if result.winner:
            print(result.winner.content)
        print(
            f"\n[{result.strategy}] agreement={result.agreement_score:.2f} {result.total_ms:.0f}ms"
        )

    elif cmd == "stats":
        print(json.dumps(engine.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
