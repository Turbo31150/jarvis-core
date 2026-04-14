#!/usr/bin/env python3
"""
jarvis_consensus_voter — Multi-model consensus via voting and weighted aggregation
Queries multiple models, applies voting strategies to pick or blend the best answer
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from enum import Enum

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.consensus_voter")

REDIS_PREFIX = "jarvis:consensus:"


class VoteStrategy(str, Enum):
    MAJORITY = "majority"  # most common answer
    WEIGHTED = "weighted"  # weighted by model quality score
    HIGHEST_CONFIDENCE = "confidence"  # pick answer with most overlap
    BORDA = "borda"  # Borda count from ranked candidates
    UNANIMOUS = "unanimous"  # only agree if all match


class ConsensusStatus(str, Enum):
    CONSENSUS = "consensus"
    PARTIAL = "partial"
    DIVERGENT = "divergent"
    UNANIMOUS = "unanimous"


@dataclass
class VoterModel:
    name: str
    url: str
    model: str
    weight: float = 1.0
    timeout_s: float = 30.0


@dataclass
class ModelVote:
    model_name: str
    content: str
    latency_ms: float
    success: bool
    weight: float = 1.0
    normalized: str = ""  # lowercased/stripped for comparison
    confidence: float = 0.0

    def to_dict(self) -> dict:
        return {
            "model": self.model_name,
            "content": self.content[:150],
            "latency_ms": round(self.latency_ms, 1),
            "success": self.success,
            "weight": self.weight,
            "confidence": round(self.confidence, 3),
        }


@dataclass
class ConsensusResult:
    strategy: VoteStrategy
    status: ConsensusStatus
    winner_content: str
    winner_model: str
    agreement_score: float  # 0-1: how much models agreed
    total_votes: int
    successful_votes: int
    votes: list[ModelVote]
    duration_ms: float

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy.value,
            "status": self.status.value,
            "winner_model": self.winner_model,
            "winner_content": self.winner_content[:300],
            "agreement_score": round(self.agreement_score, 3),
            "total_votes": self.total_votes,
            "successful_votes": self.successful_votes,
            "duration_ms": round(self.duration_ms, 1),
            "votes": [v.to_dict() for v in self.votes],
        }


def _normalize(text: str) -> str:
    """Normalize for comparison: lowercase, strip, collapse whitespace."""
    return re.sub(r"\s+", " ", text.lower().strip())[:200]


def _similarity(a: str, b: str) -> float:
    """Simple Jaccard similarity on word sets."""
    wa = set(a.split())
    wb = set(b.split())
    if not wa and not wb:
        return 1.0
    return len(wa & wb) / max(len(wa | wb), 1)


def _confidence(vote: ModelVote, all_votes: list[ModelVote]) -> float:
    """Confidence = average similarity with other successful votes."""
    others = [v for v in all_votes if v.success and v is not vote]
    if not others:
        return 1.0
    return sum(_similarity(vote.normalized, v.normalized) for v in others) / len(others)


class ConsensusVoter:
    def __init__(self, strategy: VoteStrategy = VoteStrategy.WEIGHTED):
        self.redis: aioredis.Redis | None = None
        self._voters: list[VoterModel] = []
        self._strategy = strategy
        self._stats: dict[str, int] = {
            "queries": 0,
            "consensus": 0,
            "divergent": 0,
            "unanimous": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def add_voter(self, voter: VoterModel):
        self._voters.append(voter)

    async def _query_model(
        self, voter: VoterModel, messages: list[dict], params: dict
    ) -> ModelVote:
        t0 = time.time()
        try:
            payload = {"model": voter.model, "messages": messages, **params}
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=voter.timeout_s)
            ) as sess:
                async with sess.post(
                    f"{voter.url}/v1/chat/completions", json=payload
                ) as r:
                    lat = (time.time() - t0) * 1000
                    if r.status == 200:
                        data = await r.json(content_type=None)
                        content = data["choices"][0]["message"]["content"]
                        return ModelVote(
                            voter.name,
                            content,
                            lat,
                            True,
                            voter.weight,
                            _normalize(content),
                        )
                    else:
                        return ModelVote(
                            voter.name,
                            "",
                            (time.time() - t0) * 1000,
                            False,
                            voter.weight,
                        )
        except Exception as e:
            return ModelVote(
                voter.name,
                "",
                (time.time() - t0) * 1000,
                False,
                voter.weight,
                error=str(e)[:80] if hasattr(ModelVote, "error") else "",
            )

    def _apply_majority(self, votes: list[ModelVote]) -> tuple[str, str, float]:
        groups: dict[str, list[ModelVote]] = {}
        for v in votes:
            key = v.normalized[:100]
            groups.setdefault(key, []).append(v)

        best_key = max(groups, key=lambda k: len(groups[k]))
        best_group = groups[best_key]
        agreement = len(best_group) / len(votes)
        best_vote = max(
            best_group,
            key=lambda v: v.latency_ms == min(vv.latency_ms for vv in best_group),
        )
        return best_vote.content, best_vote.model_name, agreement

    def _apply_weighted(self, votes: list[ModelVote]) -> tuple[str, str, float]:
        groups: dict[str, list[ModelVote]] = {}
        for v in votes:
            key = v.normalized[:100]
            groups.setdefault(key, []).append(v)

        group_weights = {k: sum(v.weight for v in grp) for k, grp in groups.items()}
        total_weight = sum(group_weights.values()) or 1.0
        best_key = max(group_weights, key=lambda k: group_weights[k])
        agreement = group_weights[best_key] / total_weight
        best_vote = max(groups[best_key], key=lambda v: v.weight)
        return best_vote.content, best_vote.model_name, agreement

    def _apply_confidence(self, votes: list[ModelVote]) -> tuple[str, str, float]:
        for v in votes:
            v.confidence = _confidence(v, votes)
        best = max(votes, key=lambda v: v.confidence)
        agreement = sum(v.confidence for v in votes) / len(votes)
        return best.content, best.model_name, agreement

    def _apply_unanimous(self, votes: list[ModelVote]) -> tuple[str, str, float, bool]:
        if len(set(v.normalized[:100] for v in votes)) == 1:
            return votes[0].content, votes[0].model_name, 1.0, True
        # Fall back to weighted
        content, model, agreement = self._apply_weighted(votes)
        return content, model, agreement, False

    async def vote(
        self,
        messages: list[dict],
        strategy: VoteStrategy | None = None,
        params: dict | None = None,
        voter_names: list[str] | None = None,
    ) -> ConsensusResult:
        t0 = time.time()
        self._stats["queries"] += 1
        strat = strategy or self._strategy
        params = params or {}

        targets = [
            v for v in self._voters if voter_names is None or v.name in voter_names
        ]
        if not targets:
            raise RuntimeError("No voters configured")

        # Query all in parallel
        raw_votes = await asyncio.gather(
            *[self._query_model(v, messages, params) for v in targets],
            return_exceptions=True,
        )
        votes = []
        for raw in raw_votes:
            if isinstance(raw, Exception):
                votes.append(ModelVote("error", "", 0, False))
            else:
                votes.append(raw)

        successful = [v for v in votes if v.success]
        if not successful:
            return ConsensusResult(
                strategy=strat,
                status=ConsensusStatus.DIVERGENT,
                winner_content="",
                winner_model="none",
                agreement_score=0.0,
                total_votes=len(votes),
                successful_votes=0,
                votes=votes,
                duration_ms=(time.time() - t0) * 1000,
            )

        unanimous_flag = False
        if strat == VoteStrategy.MAJORITY:
            content, model, agreement = self._apply_majority(successful)
        elif strat == VoteStrategy.WEIGHTED:
            content, model, agreement = self._apply_weighted(successful)
        elif strat == VoteStrategy.HIGHEST_CONFIDENCE:
            content, model, agreement = self._apply_confidence(successful)
        elif strat == VoteStrategy.UNANIMOUS:
            content, model, agreement, unanimous_flag = self._apply_unanimous(
                successful
            )
        else:
            content, model, agreement = self._apply_weighted(successful)

        if unanimous_flag:
            status = ConsensusStatus.UNANIMOUS
            self._stats["unanimous"] += 1
        elif agreement >= 0.7:
            status = ConsensusStatus.CONSENSUS
            self._stats["consensus"] += 1
        elif agreement >= 0.4:
            status = ConsensusStatus.PARTIAL
        else:
            status = ConsensusStatus.DIVERGENT
            self._stats["divergent"] += 1

        result = ConsensusResult(
            strategy=strat,
            status=status,
            winner_content=content,
            winner_model=model,
            agreement_score=agreement,
            total_votes=len(votes),
            successful_votes=len(successful),
            votes=votes,
            duration_ms=(time.time() - t0) * 1000,
        )

        if self.redis:
            asyncio.create_task(
                self.redis.setex(
                    f"{REDIS_PREFIX}last", 300, json.dumps(result.to_dict())
                )
            )

        return result

    def stats(self) -> dict:
        return {
            **self._stats,
            "voters": len(self._voters),
            "strategy": self._strategy.value,
        }


def build_jarvis_consensus() -> ConsensusVoter:
    voter = ConsensusVoter(VoteStrategy.WEIGHTED)
    voter.add_voter(
        VoterModel("m1-qwen9b", "http://192.168.1.85:1234", "qwen3.5-9b", weight=1.0)
    )
    voter.add_voter(
        VoterModel("m2-qwen9b", "http://192.168.1.26:1234", "qwen3.5-9b", weight=0.9)
    )
    voter.add_voter(
        VoterModel("ol1-gemma", "http://127.0.0.1:11434", "gemma3:4b", weight=0.6)
    )
    return voter


async def main():
    import sys

    voter = build_jarvis_consensus()
    await voter.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        messages = [
            {"role": "user", "content": "What is 2+2? Reply with just the number."}
        ]
        for strat in [VoteStrategy.MAJORITY, VoteStrategy.WEIGHTED]:
            print(f"\nStrategy: {strat.value}")
            try:
                result = await voter.vote(messages, strategy=strat)
                icon = {
                    "consensus": "✅",
                    "partial": "⚠️",
                    "divergent": "❌",
                    "unanimous": "🏆",
                }.get(result.status.value, "?")
                print(
                    f"  {icon} Status: {result.status.value}  Agreement: {result.agreement_score:.2f}"
                )
                print(
                    f"  Winner: {result.winner_model} → '{result.winner_content[:80]}'"
                )
            except Exception as e:
                print(f"  Error: {e}")

        print(f"\nStats: {json.dumps(voter.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(voter.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
