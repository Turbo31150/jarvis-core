#!/usr/bin/env python3
"""
jarvis_consensus_router — Multi-model consensus for high-stakes decisions
Queries N models in parallel, aggregates responses, selects best answer
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.consensus_router")

REDIS_KEY = "jarvis:consensus"

CONSENSUS_BACKENDS = [
    {
        "name": "M1_qwen9b",
        "url": "http://127.0.0.1:1234",
        "model": "qwen/qwen3.5-9b",
        "weight": 1.0,
    },
    {
        "name": "M2_qwen9b",
        "url": "http://192.168.1.26:1234",
        "model": "qwen/qwen3.5-9b",
        "weight": 1.0,
    },
    {
        "name": "M1_35b",
        "url": "http://127.0.0.1:1234",
        "model": "qwen/qwen3.5-35b-a3b",
        "weight": 1.5,
    },
]

JUDGE_PROMPT = """Tu reçois {n} réponses à la même question. Analyse-les et sélectionne la meilleure.

Question: {question}

Réponses:
{responses}

Réponds UNIQUEMENT avec un JSON: {{"best_index": <0-{max_idx}>, "reason": "<brève raison>", "consensus_score": <0.0-1.0>}}
consensus_score = 1.0 si toutes les réponses sont identiques, 0.0 si contradictoires."""


@dataclass
class ConsensusVote:
    backend_name: str
    model: str
    response: str
    latency_s: float
    weight: float = 1.0
    ok: bool = True
    error: str = ""


@dataclass
class ConsensusResult:
    question: str
    votes: list[ConsensusVote] = field(default_factory=list)
    winner: ConsensusVote | None = None
    consensus_score: float = 0.0
    judge_reason: str = ""
    total_latency_s: float = 0.0
    strategy: str = "majority"

    def to_dict(self) -> dict:
        return {
            "question": self.question[:200],
            "consensus_score": round(self.consensus_score, 3),
            "strategy": self.strategy,
            "winner": {
                "backend": self.winner.backend_name if self.winner else None,
                "model": self.winner.model if self.winner else None,
                "response": self.winner.response[:500] if self.winner else None,
            },
            "judge_reason": self.judge_reason,
            "total_latency_s": round(self.total_latency_s, 3),
            "vote_count": len([v for v in self.votes if v.ok]),
        }


class ConsensusRouter:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._judge_url = "http://127.0.0.1:1234"
        self._judge_model = "qwen/qwen3.5-9b"

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    async def _query_one(
        self,
        backend: dict,
        prompt: str,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> ConsensusVote:
        t0 = time.time()
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60)
            ) as sess:
                async with sess.post(
                    f"{backend['url']}/v1/chat/completions",
                    json={
                        "model": backend["model"],
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                    },
                ) as r:
                    data = await r.json()

            content = data["choices"][0]["message"]["content"]
            return ConsensusVote(
                backend_name=backend["name"],
                model=backend["model"],
                response=content,
                latency_s=round(time.time() - t0, 3),
                weight=backend.get("weight", 1.0),
            )
        except Exception as e:
            return ConsensusVote(
                backend_name=backend["name"],
                model=backend["model"],
                response="",
                latency_s=round(time.time() - t0, 3),
                weight=backend.get("weight", 1.0),
                ok=False,
                error=str(e)[:100],
            )

    async def _judge(
        self, question: str, votes: list[ConsensusVote]
    ) -> tuple[int, float, str]:
        """Use LLM judge to select best response."""
        ok_votes = [v for v in votes if v.ok]
        if not ok_votes:
            return 0, 0.0, "no valid responses"
        if len(ok_votes) == 1:
            return votes.index(ok_votes[0]), 1.0, "only one valid response"

        responses_text = "\n\n".join(
            f"Réponse {i}: [{v.backend_name}]\n{v.response[:300]}"
            for i, v in enumerate(ok_votes)
        )
        prompt = JUDGE_PROMPT.format(
            n=len(ok_votes),
            question=question,
            responses=responses_text,
            max_idx=len(ok_votes) - 1,
        )

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            ) as sess:
                async with sess.post(
                    f"{self._judge_url}/v1/chat/completions",
                    json={
                        "model": self._judge_model,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 100,
                        "temperature": 0,
                    },
                ) as r:
                    data = await r.json()

            content = data["choices"][0]["message"]["content"]
            import re

            m = re.search(r"\{.*\}", content, re.DOTALL)
            if m:
                result = json.loads(m.group())
                idx = int(result.get("best_index", 0))
                score = float(result.get("consensus_score", 0.5))
                reason = result.get("reason", "")
                # Map back to original votes index
                original_idx = votes.index(ok_votes[min(idx, len(ok_votes) - 1)])
                return original_idx, score, reason
        except Exception as e:
            log.warning(f"Judge error: {e}")

        # Fallback: pick fastest
        fastest = min(ok_votes, key=lambda v: v.latency_s)
        return votes.index(fastest), 0.5, "fallback: fastest"

    async def query(
        self,
        question: str,
        backends: list[dict] | None = None,
        strategy: str = "judge",  # judge | fastest | majority
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> ConsensusResult:
        backends = backends or CONSENSUS_BACKENDS
        t0 = time.time()

        # Query all backends in parallel
        tasks = [
            self._query_one(b, question, max_tokens, temperature) for b in backends
        ]
        votes = await asyncio.gather(*tasks)

        result = ConsensusResult(
            question=question,
            votes=list(votes),
            strategy=strategy,
            total_latency_s=round(time.time() - t0, 3),
        )

        ok_votes = [v for v in votes if v.ok]
        if not ok_votes:
            return result

        if strategy == "fastest":
            result.winner = min(ok_votes, key=lambda v: v.latency_s)
            result.consensus_score = 0.5

        elif strategy == "judge":
            best_idx, score, reason = await self._judge(question, list(votes))
            result.winner = votes[best_idx] if best_idx < len(votes) else ok_votes[0]
            result.consensus_score = score
            result.judge_reason = reason

        elif strategy == "majority":
            # Simple: pick most common response by overlap
            responses = [v.response for v in ok_votes]
            best = max(
                ok_votes,
                key=lambda v: sum(
                    1
                    for other in responses
                    if v.response[:100] in other or other[:100] in v.response
                ),
            )
            result.winner = best
            result.consensus_score = 0.7

        if self.redis:
            await self.redis.lpush(
                REDIS_KEY,
                json.dumps(result.to_dict()),
            )
            await self.redis.ltrim(REDIS_KEY, 0, 99)

        return result

    async def history(self, limit: int = 10) -> list[dict]:
        if not self.redis:
            return []
        items = await self.redis.lrange(REDIS_KEY, 0, limit - 1)
        return [json.loads(i) for i in items]


# ── CLI ───────────────────────────────────────────────────────────────────────


async def main():
    import sys

    router = ConsensusRouter()
    await router.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"

    if cmd == "query" and len(sys.argv) > 2:
        question = " ".join(sys.argv[2:])
        strategy = "judge"
        if "--fastest" in sys.argv:
            strategy = "fastest"
        elif "--majority" in sys.argv:
            strategy = "majority"

        print(f"Querying {len(CONSENSUS_BACKENDS)} backends...")
        result = await router.query(question, strategy=strategy)

        print(f"\nConsensus: {result.consensus_score:.2f} | Strategy: {strategy}")
        print(
            f"Winner: [{result.winner.backend_name}] ({result.winner.latency_s:.2f}s)"
        )
        if result.judge_reason:
            print(f"Reason: {result.judge_reason}")
        print(f"\n{result.winner.response}")
        print("\n--- Vote summary ---")
        for v in result.votes:
            icon = "✅" if v.ok else "❌"
            print(f"  {icon} [{v.backend_name}] {v.latency_s:.2f}s")

    elif cmd == "history":
        items = await router.history()
        for item in items:
            print(
                f"  score={item['consensus_score']:.2f} [{item['winner']['backend']}] "
                f"{item['question'][:60]}"
            )

    else:
        print(
            "Usage: jarvis_consensus_router.py query <question> [--fastest|--majority]"
        )
        print("       jarvis_consensus_router.py history")


if __name__ == "__main__":
    asyncio.run(main())
