#!/usr/bin/env python3
"""
jarvis_shadow_mode — Shadow traffic routing for safe model comparison
Routes requests to both prod and candidate models, compares outputs without affecting users
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.shadow_mode")

LM_URL = "http://127.0.0.1:1234"
SHADOW_DIR = Path("/home/turbo/IA/Core/jarvis/data/shadow")
REDIS_PREFIX = "jarvis:shadow:"


@dataclass
class ShadowResult:
    request_id: str
    prompt: str
    prod_model: str
    shadow_model: str
    prod_response: str
    shadow_response: str
    prod_latency_ms: float
    shadow_latency_ms: float
    similarity: float  # 0-1 word overlap
    diverged: bool  # True if responses differ significantly
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "prompt": self.prompt[:200],
            "prod_model": self.prod_model,
            "shadow_model": self.shadow_model,
            "prod_response": self.prod_response[:500],
            "shadow_response": self.shadow_response[:500],
            "prod_latency_ms": self.prod_latency_ms,
            "shadow_latency_ms": self.shadow_latency_ms,
            "similarity": self.similarity,
            "diverged": self.diverged,
            "ts": self.ts,
        }


def _word_similarity(a: str, b: str) -> float:
    wa = set(a.lower().split())
    wb = set(b.lower().split())
    if not wa and not wb:
        return 1.0
    return len(wa & wb) / max(len(wa | wb), 1)


class ShadowMode:
    def __init__(
        self,
        prod_model: str = "qwen/qwen3.5-9b",
        shadow_model: str = "qwen/qwen3.5-27b-claude-4.6-opus-distilled",
        divergence_threshold: float = 0.4,
        sample_rate: float = 1.0,
    ):
        self.prod_model = prod_model
        self.shadow_model = shadow_model
        self.divergence_threshold = divergence_threshold
        self.sample_rate = sample_rate
        self.redis: aioredis.Redis | None = None
        self._results: list[ShadowResult] = []
        SHADOW_DIR.mkdir(parents=True, exist_ok=True)

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    async def _call(
        self, model: str, messages: list[dict], max_tokens: int = 512
    ) -> tuple[str, float]:
        t0 = time.time()
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60)
            ) as sess:
                async with sess.post(
                    f"{LM_URL}/v1/chat/completions",
                    json={
                        "model": model,
                        "messages": messages,
                        "max_tokens": max_tokens,
                        "temperature": 0.7,
                    },
                ) as r:
                    latency = (time.time() - t0) * 1000
                    if r.status == 200:
                        data = await r.json()
                        return data["choices"][0]["message"]["content"].strip(), latency
        except Exception as e:
            log.warning(f"Shadow call failed ({model}): {e}")
        return "", (time.time() - t0) * 1000

    async def run(
        self,
        messages: list[dict],
        max_tokens: int = 512,
    ) -> tuple[str, ShadowResult | None]:
        """
        Run prod + shadow in parallel.
        Returns (prod_response, shadow_result).
        User sees prod_response; shadow_result is for analysis only.
        """
        import random

        prompt = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "user"),
            "",
        )

        # Parallel inference
        prod_task = asyncio.create_task(
            self._call(self.prod_model, messages, max_tokens)
        )
        shadow_response, shadow_latency = "", 0.0

        if random.random() < self.sample_rate:
            shadow_task = asyncio.create_task(
                self._call(self.shadow_model, messages, max_tokens)
            )
            (
                (prod_response, prod_latency),
                (shadow_response, shadow_latency),
            ) = await asyncio.gather(prod_task, shadow_task)
        else:
            prod_response, prod_latency = await prod_task

        if not shadow_response:
            return prod_response, None

        similarity = _word_similarity(prod_response, shadow_response)
        diverged = similarity < self.divergence_threshold

        result = ShadowResult(
            request_id=str(uuid.uuid4())[:8],
            prompt=prompt,
            prod_model=self.prod_model,
            shadow_model=self.shadow_model,
            prod_response=prod_response,
            shadow_response=shadow_response,
            prod_latency_ms=round(prod_latency, 1),
            shadow_latency_ms=round(shadow_latency, 1),
            similarity=round(similarity, 3),
            diverged=diverged,
        )

        self._results.append(result)
        if len(self._results) > 1000:
            self._results = self._results[-1000:]

        await self._persist(result)

        if diverged:
            log.info(
                f"Shadow diverged [{result.request_id}] sim={similarity:.2f} "
                f"prod={prod_latency:.0f}ms shadow={shadow_latency:.0f}ms"
            )

        return prod_response, result

    async def _persist(self, result: ShadowResult):
        log_file = SHADOW_DIR / f"shadow_{time.strftime('%Y%m%d')}.jsonl"
        with open(log_file, "a") as f:
            f.write(json.dumps(result.to_dict()) + "\n")

        if self.redis:
            await self.redis.lpush(
                f"{REDIS_PREFIX}results", json.dumps(result.to_dict())
            )
            await self.redis.ltrim(f"{REDIS_PREFIX}results", 0, 4999)
            if result.diverged:
                await self.redis.incr(f"{REDIS_PREFIX}diverged_count")
            await self.redis.incr(f"{REDIS_PREFIX}total_count")

    def report(self) -> dict:
        if not self._results:
            return {"total": 0}
        diverged = [r for r in self._results if r.diverged]
        return {
            "total": len(self._results),
            "diverged": len(diverged),
            "diverged_pct": round(len(diverged) / len(self._results) * 100, 1),
            "avg_similarity": round(mean(r.similarity for r in self._results), 3),
            "prod_avg_ms": round(mean(r.prod_latency_ms for r in self._results), 1),
            "shadow_avg_ms": round(mean(r.shadow_latency_ms for r in self._results), 1),
            "prod_model": self.prod_model,
            "shadow_model": self.shadow_model,
        }


async def main():
    import sys

    shadow = ShadowMode()
    await shadow.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        prompts = [
            "What is Redis?",
            "Explain async/await in Python.",
            "How does GPU VRAM work?",
        ]
        for p in prompts:
            messages = [{"role": "user", "content": p}]
            prod_resp, result = await shadow.run(messages, max_tokens=150)
            if result:
                div = "⚠️  DIVERGED" if result.diverged else "✅ similar"
                print(
                    f"{div} sim={result.similarity:.2f} prod={result.prod_latency_ms:.0f}ms shadow={result.shadow_latency_ms:.0f}ms"
                )
                print(f"  P: {prod_resp[:80]}")
                print(f"  S: {result.shadow_response[:80]}\n")

        r = shadow.report()
        print(
            f"\nReport: {r['diverged']}/{r['total']} diverged ({r['diverged_pct']}%) avg_sim={r['avg_similarity']}"
        )

    elif cmd == "report":
        r = shadow.report()
        print(json.dumps(r, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
