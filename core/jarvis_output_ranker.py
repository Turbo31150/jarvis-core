#!/usr/bin/env python3
"""
jarvis_output_ranker — Multi-criteria ranking of LLM outputs for consensus selection
Scores responses on quality, relevance, length, coherence, and confidence signals
"""

import asyncio
import json
import logging
import math
import re
from dataclasses import dataclass, field
from enum import Enum

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.output_ranker")

REDIS_PREFIX = "jarvis:ranker:"


class RankCriteria(str, Enum):
    LENGTH = "length"  # prefer responses of target length
    CONFIDENCE = "confidence"  # higher confidence signals
    SPECIFICITY = "specificity"  # concrete details vs vague
    COHERENCE = "coherence"  # structural quality
    RELEVANCE = "relevance"  # keyword overlap with query
    LATENCY = "latency"  # prefer faster responses


@dataclass
class RankWeights:
    length: float = 0.15
    confidence: float = 0.25
    specificity: float = 0.20
    coherence: float = 0.20
    relevance: float = 0.15
    latency: float = 0.05

    def normalize(self) -> "RankWeights":
        total = (
            self.length
            + self.confidence
            + self.specificity
            + self.coherence
            + self.relevance
            + self.latency
        )
        if total == 0:
            return self
        return RankWeights(
            length=self.length / total,
            confidence=self.confidence / total,
            specificity=self.specificity / total,
            coherence=self.coherence / total,
            relevance=self.relevance / total,
            latency=self.latency / total,
        )


@dataclass
class CandidateOutput:
    candidate_id: str
    text: str
    model: str = ""
    latency_ms: float = 0.0
    metadata: dict = field(default_factory=dict)


@dataclass
class ScoredOutput:
    candidate: CandidateOutput
    total_score: float
    subscores: dict[str, float] = field(default_factory=dict)
    rank: int = 0

    def to_dict(self) -> dict:
        return {
            "candidate_id": self.candidate.candidate_id,
            "model": self.candidate.model,
            "total_score": round(self.total_score, 4),
            "subscores": {k: round(v, 4) for k, v in self.subscores.items()},
            "rank": self.rank,
            "text_preview": self.candidate.text[:100],
        }


# ---- Scoring functions ----

_CONFIDENCE_BOOSTERS = re.compile(
    r"\b(specifically|precisely|exactly|confirmed|verified|measured|"
    r"according to|based on|research shows|data indicates)\b",
    re.I,
)
_CONFIDENCE_DETRACTORS = re.compile(
    r"\b(maybe|perhaps|possibly|I think|I believe|might be|not sure|"
    r"uncertain|could be|I'm not certain|hard to say)\b",
    re.I,
)
_VAGUE_PHRASES = re.compile(
    r"\b(some|various|certain|things|stuff|etc|and so on|and more|"
    r"many|few|several)\b",
    re.I,
)
_CONCRETE_PATTERNS = re.compile(
    r"\b(\d+[\.,]?\d*\s*(%|ms|MB|GB|KB|s|min|hours?|days?|tokens?)|"
    r"http[s]?://\S+|[A-Z][a-z]+\d+|v\d+\.\d+)\b"
)
_BULLET_OR_LIST = re.compile(r"^[\s]*[-*•]\s+.+$", re.MULTILINE)
_CODE_BLOCK = re.compile(r"```[\s\S]+?```")
_SENTENCE_END = re.compile(r"[.!?]\s+")


def _score_length(text: str, target_min: int = 50, target_max: int = 500) -> float:
    n = len(text.split())
    if n < 5:
        return 0.1
    if n < target_min:
        return 0.4 + 0.6 * (n / target_min)
    if n <= target_max:
        return 1.0
    # Penalize very long responses gradually
    return max(0.3, 1.0 - (n - target_max) / target_max * 0.5)


def _score_confidence(text: str) -> float:
    boosts = len(_CONFIDENCE_BOOSTERS.findall(text))
    detract = len(_CONFIDENCE_DETRACTORS.findall(text))
    words = max(len(text.split()), 1)
    score = 0.5 + (boosts - detract * 1.5) / max(words / 20, 1)
    return max(0.0, min(1.0, score))


def _score_specificity(text: str) -> float:
    vague = len(_VAGUE_PHRASES.findall(text))
    concrete = len(_CONCRETE_PATTERNS.findall(text))
    words = max(len(text.split()), 1)
    score = 0.4 + (concrete * 2 - vague) / max(words / 10, 1) * 0.1
    return max(0.0, min(1.0, score))


def _score_coherence(text: str) -> float:
    score = 0.5
    sentences = _SENTENCE_END.split(text.strip())
    if len(sentences) >= 2:
        score += 0.1
    if _BULLET_OR_LIST.search(text):
        score += 0.15
    if _CODE_BLOCK.search(text):
        score += 0.1
    # Penalize very short or truncated text
    if not text.strip().endswith((".", "!", "?", "```", ":")):
        score -= 0.1
    # Check for repeated phrases (copy-paste artifacts)
    words = text.lower().split()
    if len(words) > 20:
        unique_ratio = len(set(words)) / len(words)
        score += (unique_ratio - 0.5) * 0.3
    return max(0.0, min(1.0, score))


def _score_relevance(text: str, query: str) -> float:
    if not query:
        return 0.5
    query_words = set(re.findall(r"\b\w{4,}\b", query.lower()))
    text_words = set(re.findall(r"\b\w{4,}\b", text.lower()))
    if not query_words:
        return 0.5
    overlap = len(query_words & text_words) / len(query_words)
    return min(1.0, overlap * 1.5)  # cap at 1.0, boost partial overlap


def _score_latency(latency_ms: float, target_ms: float = 1000.0) -> float:
    if latency_ms <= 0:
        return 0.5
    ratio = latency_ms / target_ms
    return max(0.0, min(1.0, 1.0 / (1.0 + math.log(max(ratio, 0.1)))))


class OutputRanker:
    def __init__(self, weights: RankWeights | None = None):
        self.redis: aioredis.Redis | None = None
        self._weights = (weights or RankWeights()).normalize()
        self._history: list[ScoredOutput] = []
        self._stats: dict[str, int] = {
            "rankings": 0,
            "candidates_scored": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def set_weights(self, weights: RankWeights):
        self._weights = weights.normalize()

    def score_one(
        self,
        candidate: CandidateOutput,
        query: str = "",
        target_length: tuple[int, int] = (50, 500),
        latency_target_ms: float = 1000.0,
    ) -> ScoredOutput:
        w = self._weights
        subscores = {
            RankCriteria.LENGTH.value: _score_length(candidate.text, *target_length),
            RankCriteria.CONFIDENCE.value: _score_confidence(candidate.text),
            RankCriteria.SPECIFICITY.value: _score_specificity(candidate.text),
            RankCriteria.COHERENCE.value: _score_coherence(candidate.text),
            RankCriteria.RELEVANCE.value: _score_relevance(candidate.text, query),
            RankCriteria.LATENCY.value: _score_latency(
                candidate.latency_ms, latency_target_ms
            ),
        }
        total = (
            w.length * subscores[RankCriteria.LENGTH.value]
            + w.confidence * subscores[RankCriteria.CONFIDENCE.value]
            + w.specificity * subscores[RankCriteria.SPECIFICITY.value]
            + w.coherence * subscores[RankCriteria.COHERENCE.value]
            + w.relevance * subscores[RankCriteria.RELEVANCE.value]
            + w.latency * subscores[RankCriteria.LATENCY.value]
        )
        return ScoredOutput(candidate=candidate, total_score=total, subscores=subscores)

    def rank(
        self,
        candidates: list[CandidateOutput],
        query: str = "",
        target_length: tuple[int, int] = (50, 500),
        latency_target_ms: float = 1000.0,
    ) -> list[ScoredOutput]:
        self._stats["rankings"] += 1
        self._stats["candidates_scored"] += len(candidates)

        scored = [
            self.score_one(c, query, target_length, latency_target_ms)
            for c in candidates
        ]
        scored.sort(key=lambda s: -s.total_score)
        for i, s in enumerate(scored):
            s.rank = i + 1

        self._history.extend(scored)
        if len(self._history) > 500:
            self._history = self._history[-500:]

        return scored

    def best(
        self,
        candidates: list[CandidateOutput],
        query: str = "",
    ) -> CandidateOutput | None:
        ranked = self.rank(candidates, query)
        return ranked[0].candidate if ranked else None

    def recent_rankings(self, n: int = 10) -> list[dict]:
        return [s.to_dict() for s in self._history[-n:]]

    def stats(self) -> dict:
        return {**self._stats}


def build_jarvis_output_ranker() -> OutputRanker:
    weights = RankWeights(
        confidence=0.30,
        specificity=0.25,
        coherence=0.20,
        relevance=0.15,
        length=0.08,
        latency=0.02,
    )
    return OutputRanker(weights=weights)


async def main():
    import sys

    ranker = build_jarvis_output_ranker()
    await ranker.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        query = "How does JARVIS route inference requests across GPU cluster nodes?"
        candidates = [
            CandidateOutput(
                "c1",
                "JARVIS uses a weighted round-robin strategy with health checks. "
                "Requests are routed to M1 (192.168.1.85) with weight 1.2 or M2 (192.168.1.26) "
                "with weight 1.0. Unhealthy nodes are removed from rotation after 3 consecutive failures.",
                "qwen3.5-9b",
                latency_ms=320,
            ),
            CandidateOutput(
                "c2",
                "It routes things to different nodes based on some criteria.",
                "gemma3:4b",
                latency_ms=90,
            ),
            CandidateOutput(
                "c3",
                "The inference gateway implements a multi-policy load balancer with "
                "WEIGHTED_LATENCY, ROUND_ROBIN, and LEAST_CONN strategies. P95 latency is tracked "
                "via a sorted 100-sample buffer. Failover triggers after 3 consecutive errors. "
                "Specifically, M1 handles 55% of traffic measured over the last 24h.",
                "deepseek-r1",
                latency_ms=850,
            ),
            CandidateOutput(
                "c4",
                "Maybe it uses some kind of load balancing? I'm not certain but I think "
                "there might be various strategies involved. Perhaps round-robin or something similar.",
                "qwen3.5-9b",
                latency_ms=280,
            ),
        ]

        ranked = ranker.rank(candidates, query=query)
        print(f"Query: '{query[:60]}...'\n")
        print("Rankings:")
        for s in ranked:
            print(
                f"  #{s.rank} [{s.candidate.model:<15}] score={s.total_score:.4f} "
                f"lat={s.candidate.latency_ms:.0f}ms"
            )
            for crit, val in s.subscores.items():
                print(f"       {crit:<15} {val:.3f}")

        print(f"\nBest: {ranker.best(candidates, query).candidate_id}")
        print(f"\nStats: {json.dumps(ranker.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(ranker.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
