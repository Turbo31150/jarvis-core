#!/usr/bin/env python3
"""
jarvis_request_multiplexer — Fan-out LLM requests to multiple nodes, merge/select best response
Sends same request to N backends in parallel, applies strategy (first/fastest/best/consensus)
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from enum import Enum

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.request_multiplexer")

REDIS_PREFIX = "jarvis:mux:"


class MergeStrategy(str, Enum):
    FIRST = "first"  # return first success
    FASTEST = "fastest"  # return fastest success
    BEST = "best"  # return highest quality score
    CONSENSUS = "consensus"  # majority vote on short answers
    ALL = "all"  # return all responses


@dataclass
class BackendConfig:
    name: str
    url: str
    model: str
    weight: float = 1.0
    timeout_s: float = 30.0
    enabled: bool = True


@dataclass
class BackendResponse:
    backend: str
    model: str
    content: str
    latency_ms: float
    success: bool
    error: str = ""
    tokens: int = 0
    quality_score: float = 0.0

    def to_dict(self) -> dict:
        return {
            "backend": self.backend,
            "model": self.model,
            "content": self.content[:200],
            "latency_ms": round(self.latency_ms, 1),
            "success": self.success,
            "error": self.error,
            "tokens": self.tokens,
            "quality_score": round(self.quality_score, 3),
        }


@dataclass
class MultiplexResult:
    strategy: MergeStrategy
    selected: BackendResponse | None
    all_responses: list[BackendResponse]
    duration_ms: float
    winner_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy.value,
            "selected": self.selected.to_dict() if self.selected else None,
            "winner_reason": self.winner_reason,
            "backend_count": len(self.all_responses),
            "success_count": sum(1 for r in self.all_responses if r.success),
            "duration_ms": round(self.duration_ms, 1),
        }


def _quality_score(content: str) -> float:
    """Heuristic: length + structure + no refusal phrases."""
    if not content:
        return 0.0
    score = min(len(content) / 500, 1.0) * 0.5
    if any(kw in content.lower() for kw in ["```", "1.", "- ", "**"]):
        score += 0.2
    if any(
        phrase in content.lower()
        for phrase in ["i cannot", "i'm unable", "i don't know"]
    ):
        score -= 0.3
    return max(0.0, min(score, 1.0))


class RequestMultiplexer:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._backends: dict[str, BackendConfig] = {}
        self._stats: dict[str, int] = {
            "requests": 0,
            "successes": 0,
            "failures": 0,
            "fallbacks": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def add_backend(self, cfg: BackendConfig):
        self._backends[cfg.name] = cfg

    def remove_backend(self, name: str):
        self._backends.pop(name, None)

    async def _call_backend(
        self, cfg: BackendConfig, messages: list[dict], params: dict
    ) -> BackendResponse:
        t0 = time.time()
        payload = {
            "model": cfg.model,
            "messages": messages,
            **{k: v for k, v in params.items() if k not in ("model",)},
        }
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=cfg.timeout_s)
            ) as sess:
                async with sess.post(
                    f"{cfg.url}/v1/chat/completions", json=payload
                ) as r:
                    lat = (time.time() - t0) * 1000
                    if r.status == 200:
                        data = await r.json(content_type=None)
                        content = data["choices"][0]["message"]["content"]
                        tokens = data.get("usage", {}).get("total_tokens", 0)
                        return BackendResponse(
                            backend=cfg.name,
                            model=cfg.model,
                            content=content,
                            latency_ms=lat,
                            success=True,
                            tokens=tokens,
                            quality_score=_quality_score(content),
                        )
                    else:
                        return BackendResponse(
                            backend=cfg.name,
                            model=cfg.model,
                            content="",
                            latency_ms=lat,
                            success=False,
                            error=f"HTTP {r.status}",
                        )
        except Exception as e:
            return BackendResponse(
                backend=cfg.name,
                model=cfg.model,
                content="",
                latency_ms=(time.time() - t0) * 1000,
                success=False,
                error=str(e)[:100],
            )

    async def multiplex(
        self,
        messages: list[dict],
        strategy: MergeStrategy = MergeStrategy.FASTEST,
        backends: list[str] | None = None,
        params: dict | None = None,
    ) -> MultiplexResult:
        t0 = time.time()
        self._stats["requests"] += 1
        params = params or {}

        targets = [
            cfg
            for name, cfg in self._backends.items()
            if cfg.enabled and (backends is None or name in backends)
        ]
        if not targets:
            self._stats["failures"] += 1
            return MultiplexResult(
                strategy=strategy,
                selected=None,
                all_responses=[],
                duration_ms=(time.time() - t0) * 1000,
                winner_reason="no backends available",
            )

        if strategy == MergeStrategy.FIRST:
            # Race — return first success
            tasks = [
                asyncio.create_task(self._call_backend(cfg, messages, params))
                for cfg in targets
            ]
            responses = []
            selected = None
            reason = "first success"
            pending = set(tasks)
            while pending and selected is None:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                for t in done:
                    r = t.result()
                    responses.append(r)
                    if r.success and selected is None:
                        selected = r
                        for p in pending:
                            p.cancel()
        else:
            # Wait for all
            responses = await asyncio.gather(
                *[self._call_backend(cfg, messages, params) for cfg in targets]
            )
            successes = [r for r in responses if r.success]

            if strategy == MergeStrategy.FASTEST:
                selected = (
                    min(successes, key=lambda r: r.latency_ms) if successes else None
                )
                reason = "lowest latency"
            elif strategy == MergeStrategy.BEST:
                selected = (
                    max(successes, key=lambda r: r.quality_score) if successes else None
                )
                reason = "highest quality score"
            elif strategy == MergeStrategy.CONSENSUS:
                selected = self._consensus(successes)
                reason = "majority consensus"
            elif strategy == MergeStrategy.ALL:
                selected = successes[0] if successes else None
                reason = "all responses"
            else:
                selected = successes[0] if successes else None
                reason = "default"

        if selected:
            self._stats["successes"] += 1
        else:
            self._stats["failures"] += 1

        return MultiplexResult(
            strategy=strategy,
            selected=selected,
            all_responses=list(responses),
            duration_ms=(time.time() - t0) * 1000,
            winner_reason=reason,
        )

    def _consensus(self, responses: list[BackendResponse]) -> BackendResponse | None:
        """Majority vote — works best for short yes/no or classification answers."""
        if not responses:
            return None
        counts: dict[str, list[BackendResponse]] = {}
        for r in responses:
            key = r.content.strip().lower()[:50]
            counts.setdefault(key, []).append(r)
        majority_key = max(counts, key=lambda k: len(counts[k]))
        group = counts[majority_key]
        # Return the one with best quality among the majority
        return max(group, key=lambda r: r.quality_score)

    def stats(self) -> dict:
        return {**self._stats, "backends": len(self._backends)}


def build_jarvis_multiplexer() -> RequestMultiplexer:
    mux = RequestMultiplexer()
    mux.add_backend(
        BackendConfig("m1-qwen", "http://192.168.1.85:1234", "qwen3.5-9b", weight=1.0)
    )
    mux.add_backend(
        BackendConfig("m2-qwen", "http://192.168.1.26:1234", "qwen3.5-9b", weight=0.8)
    )
    mux.add_backend(
        BackendConfig("ol1-gemma", "http://127.0.0.1:11434", "gemma3:4b", weight=0.6)
    )
    return mux


async def main():
    import sys

    mux = build_jarvis_multiplexer()
    await mux.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        messages = [
            {"role": "user", "content": "What is 2+2? Answer with just the number."}
        ]
        for strategy in [MergeStrategy.FASTEST, MergeStrategy.BEST]:
            print(f"\nStrategy: {strategy.value}")
            result = await mux.multiplex(messages, strategy=strategy)
            print(f"  Winner: {result.selected.backend if result.selected else 'none'}")
            print(f"  Reason: {result.winner_reason}")
            print(f"  Duration: {result.duration_ms:.0f}ms")
        print(f"\nStats: {json.dumps(mux.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(mux.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
