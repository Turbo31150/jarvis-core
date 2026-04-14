#!/usr/bin/env python3
"""
jarvis_shadow_tester — Shadow traffic testing for LLM model comparison
Mirror live requests to a shadow backend, compare responses without affecting users
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.shadow_tester")

RESULTS_FILE = Path("/home/turbo/IA/Core/jarvis/data/shadow_results.jsonl")
REDIS_PREFIX = "jarvis:shadow:"


class CompareMethod(str, Enum):
    EXACT = "exact"
    CONTAINS = "contains"
    SEMANTIC = "semantic"  # requires embedding similarity
    LENGTH = "length"  # compare response length ratio
    LATENCY_ONLY = "latency_only"


@dataclass
class ShadowResult:
    result_id: str
    request_id: str
    primary_url: str
    shadow_url: str
    model_primary: str
    model_shadow: str
    prompt: str
    primary_response: str
    shadow_response: str
    primary_latency_ms: float
    shadow_latency_ms: float
    primary_tokens: int
    shadow_tokens: int
    match_score: float  # 0.0–1.0
    divergent: bool
    ts: float = field(default_factory=time.time)
    error_primary: str = ""
    error_shadow: str = ""

    @property
    def latency_delta_ms(self) -> float:
        return self.shadow_latency_ms - self.primary_latency_ms

    def to_dict(self) -> dict:
        return {
            "result_id": self.result_id,
            "request_id": self.request_id,
            "model_primary": self.model_primary,
            "model_shadow": self.model_shadow,
            "primary_latency_ms": round(self.primary_latency_ms, 1),
            "shadow_latency_ms": round(self.shadow_latency_ms, 1),
            "latency_delta_ms": round(self.latency_delta_ms, 1),
            "primary_tokens": self.primary_tokens,
            "shadow_tokens": self.shadow_tokens,
            "match_score": round(self.match_score, 4),
            "divergent": self.divergent,
            "ts": self.ts,
            "error_primary": self.error_primary[:80] if self.error_primary else "",
            "error_shadow": self.error_shadow[:80] if self.error_shadow else "",
        }


async def _call_backend(
    url: str,
    model: str,
    messages: list[dict],
    max_tokens: int = 512,
    timeout_s: float = 30.0,
) -> tuple[str, float, int, str]:
    """Returns (response_text, latency_ms, tokens, error)."""
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.0,
    }
    t0 = time.time()
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout_s)
        ) as sess:
            async with sess.post(f"{url}/v1/chat/completions", json=payload) as r:
                latency_ms = (time.time() - t0) * 1000
                if r.status != 200:
                    text = await r.text()
                    return "", latency_ms, 0, f"HTTP {r.status}: {text[:80]}"
                data = await r.json(content_type=None)
                content = (
                    data.get("choices", [{}])[0].get("message", {}).get("content", "")
                )
                usage = data.get("usage", {})
                tokens = usage.get("completion_tokens", max(1, len(content) // 4))
                return content, latency_ms, tokens, ""
    except Exception as e:
        return "", (time.time() - t0) * 1000, 0, str(e)[:120]


def _compare_responses(
    a: str, b: str, method: CompareMethod, threshold: float = 0.8
) -> tuple[float, bool]:
    """Returns (match_score, is_divergent)."""
    if method == CompareMethod.EXACT:
        score = 1.0 if a.strip() == b.strip() else 0.0
    elif method == CompareMethod.CONTAINS:
        # Check if key sentences from a appear in b
        sentences_a = [s.strip() for s in a.split(".") if len(s.strip()) > 10]
        if not sentences_a:
            score = 1.0 if a.strip() == b.strip() else 0.0
        else:
            hits = sum(1 for s in sentences_a if s.lower()[:30] in b.lower())
            score = hits / len(sentences_a)
    elif method == CompareMethod.LENGTH:
        len_a = max(len(a), 1)
        len_b = max(len(b), 1)
        ratio = min(len_a, len_b) / max(len_a, len_b)
        score = ratio
    elif method == CompareMethod.LATENCY_ONLY:
        score = 1.0  # always match, we only care about latency
    else:
        score = 1.0

    return score, score < threshold


class ShadowTester:
    def __init__(
        self,
        primary_url: str,
        primary_model: str,
        shadow_url: str,
        shadow_model: str,
        compare_method: CompareMethod = CompareMethod.LENGTH,
        divergence_threshold: float = 0.7,
        sample_rate: float = 1.0,  # 0.0–1.0, fraction of requests to shadow
        persist: bool = True,
    ):
        self.redis: aioredis.Redis | None = None
        self.primary_url = primary_url
        self.primary_model = primary_model
        self.shadow_url = shadow_url
        self.shadow_model = shadow_model
        self.compare_method = compare_method
        self.divergence_threshold = divergence_threshold
        self.sample_rate = sample_rate
        self._results: list[ShadowResult] = []
        self._max_results = 5_000
        self._persist = persist
        self._stats: dict[str, Any] = {
            "requests": 0,
            "shadowed": 0,
            "divergent": 0,
            "errors_primary": 0,
            "errors_shadow": 0,
        }
        if persist:
            RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def _should_shadow(self) -> bool:
        import random

        return random.random() < self.sample_rate

    async def request(
        self,
        messages: list[dict],
        max_tokens: int = 512,
        request_id: str = "",
    ) -> tuple[str, ShadowResult | None]:
        """
        Send to primary (always). Optionally mirror to shadow.
        Returns (primary_response, shadow_result_or_None).
        """
        self._stats["requests"] += 1
        req_id = request_id or str(uuid.uuid4())[:8]

        # Always call primary
        primary_coro = _call_backend(
            self.primary_url, self.primary_model, messages, max_tokens
        )

        prompt_text = " ".join(m.get("content", "") for m in messages)[:200]

        if self._should_shadow():
            self._stats["shadowed"] += 1
            shadow_coro = _call_backend(
                self.shadow_url, self.shadow_model, messages, max_tokens
            )
            (
                (p_resp, p_lat, p_tok, p_err),
                (s_resp, s_lat, s_tok, s_err),
            ) = await asyncio.gather(primary_coro, shadow_coro)
        else:
            p_resp, p_lat, p_tok, p_err = await primary_coro
            s_resp, s_lat, s_tok, s_err = "", 0.0, 0, ""

        if p_err:
            self._stats["errors_primary"] += 1
        if s_err:
            self._stats["errors_shadow"] += 1

        shadow_result = None
        if self._stats["shadowed"] > 0 and s_resp or s_err:
            match_score, divergent = _compare_responses(
                p_resp, s_resp, self.compare_method, self.divergence_threshold
            )
            if divergent:
                self._stats["divergent"] += 1

            shadow_result = ShadowResult(
                result_id=str(uuid.uuid4())[:8],
                request_id=req_id,
                primary_url=self.primary_url,
                shadow_url=self.shadow_url,
                model_primary=self.primary_model,
                model_shadow=self.shadow_model,
                prompt=prompt_text,
                primary_response=p_resp[:500],
                shadow_response=s_resp[:500],
                primary_latency_ms=p_lat,
                shadow_latency_ms=s_lat,
                primary_tokens=p_tok,
                shadow_tokens=s_tok,
                match_score=match_score,
                divergent=divergent,
                error_primary=p_err,
                error_shadow=s_err,
            )

            self._results.append(shadow_result)
            if len(self._results) > self._max_results:
                self._results.pop(0)

            if self._persist:
                try:
                    with open(RESULTS_FILE, "a") as f:
                        f.write(json.dumps(shadow_result.to_dict()) + "\n")
                except Exception:
                    pass

            if self.redis:
                asyncio.create_task(self._redis_push(shadow_result))

        return p_resp, shadow_result

    async def _redis_push(self, result: ShadowResult):
        if not self.redis:
            return
        try:
            await self.redis.xadd(
                f"{REDIS_PREFIX}stream",
                result.to_dict(),
                maxlen=10_000,
            )
        except Exception:
            pass

    def summary(self, n: int = 100) -> dict:
        recent = self._results[-n:]
        if not recent:
            return {"count": 0}
        divergent = [r for r in recent if r.divergent]
        avg_match = sum(r.match_score for r in recent) / len(recent)
        avg_lat_delta = sum(r.latency_delta_ms for r in recent) / len(recent)
        return {
            "count": len(recent),
            "divergent": len(divergent),
            "divergence_rate": round(len(divergent) / len(recent), 4),
            "avg_match_score": round(avg_match, 4),
            "avg_latency_delta_ms": round(avg_lat_delta, 1),
            "primary": self.primary_model,
            "shadow": self.shadow_model,
        }

    def recent_divergent(self, limit: int = 10) -> list[dict]:
        return [r.to_dict() for r in self._results if r.divergent][-limit:]

    def stats(self) -> dict:
        return {
            **self._stats,
            "results_stored": len(self._results),
            "divergence_rate": round(
                self._stats["divergent"] / max(self._stats["shadowed"], 1), 4
            ),
        }


def build_jarvis_shadow_tester(
    primary_url: str = "http://192.168.1.85:1234",
    primary_model: str = "qwen3.5-9b",
    shadow_url: str = "http://192.168.1.26:1234",
    shadow_model: str = "qwen3.5-9b",
) -> ShadowTester:
    return ShadowTester(
        primary_url=primary_url,
        primary_model=primary_model,
        shadow_url=shadow_url,
        shadow_model=shadow_model,
        compare_method=CompareMethod.LENGTH,
        divergence_threshold=0.6,
        sample_rate=0.1,  # shadow 10% of traffic
    )


async def main():
    import sys

    tester = build_jarvis_shadow_tester()
    await tester.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"

    if cmd == "stats":
        print(json.dumps(tester.stats(), indent=2))

    elif cmd == "summary":
        print(json.dumps(tester.summary(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
