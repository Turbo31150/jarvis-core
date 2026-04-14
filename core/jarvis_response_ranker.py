#!/usr/bin/env python3
"""
jarvis_response_ranker — Multi-criteria ranking of LLM candidate responses
Relevance, coherence, factuality, safety, diversity scoring + ensemble ranking
"""

import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

log = logging.getLogger("jarvis.response_ranker")


class RankCriterion(str, Enum):
    RELEVANCE = "relevance"  # how well it answers the query
    COHERENCE = "coherence"  # logical flow and consistency
    COMPLETENESS = "completeness"  # covers all aspects of the query
    CONCISENESS = "conciseness"  # not overly verbose
    SAFETY = "safety"  # no harmful content
    FACTUALITY = "factuality"  # consistency with known facts
    DIVERSITY = "diversity"  # penalize near-duplicates
    CONFIDENCE = "confidence"  # model self-confidence signals
    CUSTOM = "custom"


@dataclass
class CriterionWeight:
    criterion: RankCriterion
    weight: float = 1.0
    enabled: bool = True


@dataclass
class CandidateScore:
    candidate_id: str
    text: str
    scores: dict[RankCriterion, float] = field(default_factory=dict)
    final_score: float = 0.0
    rank: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "rank": self.rank,
            "final_score": round(self.final_score, 4),
            "text_preview": self.text[:100],
            "scores": {k.value: round(v, 4) for k, v in self.scores.items()},
        }


@dataclass
class RankingResult:
    query: str
    candidates: list[CandidateScore]
    best: CandidateScore
    duration_ms: float = 0.0
    weights_used: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "query_preview": self.query[:80],
            "num_candidates": len(self.candidates),
            "best_id": self.best.candidate_id,
            "best_score": round(self.best.final_score, 4),
            "rankings": [c.to_dict() for c in self.candidates],
            "duration_ms": round(self.duration_ms, 2),
        }


# --- Heuristic scorers ---

_UNSAFE_PATTERNS = re.compile(
    r"\b(kill|murder|bomb|hack|exploit|bypass|illegal|weapon|drug|violence)\b",
    re.I,
)
_REFUSAL_PATTERNS = re.compile(
    r"\b(I cannot|I can't|I won't|I'm not able|I am unable|I'm unable)\b",
    re.I,
)
_CONFIDENCE_HEDGES = re.compile(
    r"\b(maybe|perhaps|possibly|I think|I believe|not sure|uncertain|might)\b",
    re.I,
)


def _score_safety(text: str) -> float:
    matches = len(_UNSAFE_PATTERNS.findall(text))
    return max(0.0, 1.0 - matches * 0.3)


def _score_coherence(text: str) -> float:
    """Proxy: sentence-to-sentence length variance (low variance = coherent)."""
    sentences = re.split(r"[.!?]+", text)
    lengths = [len(s.split()) for s in sentences if s.strip()]
    if len(lengths) < 2:
        return 0.7
    mean = sum(lengths) / len(lengths)
    variance = sum((l - mean) ** 2 for l in lengths) / len(lengths)
    cv = math.sqrt(variance) / max(mean, 1)  # coefficient of variation
    return max(0.0, 1.0 - cv * 0.4)


def _score_conciseness(text: str, query: str) -> float:
    """Penalize excessive length relative to query length."""
    q_len = max(len(query.split()), 1)
    r_len = len(text.split())
    ratio = r_len / q_len
    if ratio < 1:
        return 0.5  # too short
    elif ratio <= 5:
        return 1.0  # ideal
    elif ratio <= 15:
        return 1.0 - (ratio - 5) * 0.04
    else:
        return 0.4


def _score_completeness(text: str, query: str) -> float:
    """Check if query keywords appear in response."""
    query_words = set(re.findall(r"\b\w{4,}\b", query.lower()))
    if not query_words:
        return 0.5
    text_lower = text.lower()
    covered = sum(1 for w in query_words if w in text_lower)
    return covered / len(query_words)


def _score_relevance(text: str, query: str) -> float:
    """TF overlap between query and response."""
    q_words = set(re.findall(r"\b\w{3,}\b", query.lower()))
    t_words = set(re.findall(r"\b\w{3,}\b", text.lower()))
    if not q_words:
        return 0.5
    overlap = len(q_words & t_words)
    precision = overlap / max(len(t_words), 1)
    recall = overlap / len(q_words)
    if precision + recall == 0:
        return 0.0
    f1 = 2 * precision * recall / (precision + recall)
    return min(1.0, f1 * 1.5)  # scale up slightly


def _score_confidence(text: str) -> float:
    """Lower score if lots of hedging language."""
    hedges = len(_CONFIDENCE_HEDGES.findall(text))
    words = max(len(text.split()), 1)
    hedge_rate = hedges / words
    return max(0.0, 1.0 - hedge_rate * 10)


def _score_diversity(text: str, others: list[str]) -> float:
    """How different is this text from others? (Jaccard distance)."""
    if not others:
        return 1.0
    words = set(re.findall(r"\b\w+\b", text.lower()))
    similarities = []
    for other in others:
        other_words = set(re.findall(r"\b\w+\b", other.lower()))
        intersection = words & other_words
        union = words | other_words
        sim = len(intersection) / max(len(union), 1)
        similarities.append(sim)
    avg_sim = sum(similarities) / len(similarities)
    return 1.0 - avg_sim


class ResponseRanker:
    def __init__(
        self,
        weights: list[CriterionWeight] | None = None,
    ):
        self._weights = weights or self._default_weights()
        self._custom_scorers: dict[str, Callable[[str, str, list[str]], float]] = {}
        self._stats: dict[str, int] = {
            "rankings": 0,
            "candidates_scored": 0,
        }

    @staticmethod
    def _default_weights() -> list[CriterionWeight]:
        return [
            CriterionWeight(RankCriterion.RELEVANCE, 3.0),
            CriterionWeight(RankCriterion.SAFETY, 2.5),
            CriterionWeight(RankCriterion.COMPLETENESS, 2.0),
            CriterionWeight(RankCriterion.COHERENCE, 1.5),
            CriterionWeight(RankCriterion.CONCISENESS, 1.0),
            CriterionWeight(RankCriterion.CONFIDENCE, 1.0),
            CriterionWeight(RankCriterion.DIVERSITY, 0.5),
        ]

    def set_weight(self, criterion: RankCriterion, weight: float):
        for cw in self._weights:
            if cw.criterion == criterion:
                cw.weight = weight
                return
        self._weights.append(CriterionWeight(criterion, weight))

    def register_scorer(
        self,
        name: str,
        fn: Callable[[str, str, list[str]], float],
        weight: float = 1.0,
    ):
        """Register a custom scorer fn(response, query, all_responses) -> float."""
        self._custom_scorers[name] = fn
        self._weights.append(CriterionWeight(RankCriterion.CUSTOM, weight))

    def _score_candidate(
        self,
        text: str,
        query: str,
        others: list[str],
    ) -> dict[RankCriterion, float]:
        return {
            RankCriterion.RELEVANCE: _score_relevance(text, query),
            RankCriterion.SAFETY: _score_safety(text),
            RankCriterion.COMPLETENESS: _score_completeness(text, query),
            RankCriterion.COHERENCE: _score_coherence(text),
            RankCriterion.CONCISENESS: _score_conciseness(text, query),
            RankCriterion.CONFIDENCE: _score_confidence(text),
            RankCriterion.DIVERSITY: _score_diversity(text, others),
        }

    def _weighted_score(self, scores: dict[RankCriterion, float]) -> float:
        total_weight = 0.0
        weighted_sum = 0.0
        for cw in self._weights:
            if not cw.enabled:
                continue
            v = scores.get(cw.criterion, 0.5)
            weighted_sum += v * cw.weight
            total_weight += cw.weight
        return weighted_sum / max(total_weight, 1e-9)

    def rank(
        self,
        query: str,
        candidates: list[str],
        candidate_ids: list[str] | None = None,
        metadata: list[dict] | None = None,
    ) -> RankingResult:
        t0 = time.time()
        self._stats["rankings"] += 1

        ids = candidate_ids or [f"c{i}" for i in range(len(candidates))]
        meta = metadata or [{} for _ in candidates]

        scored: list[CandidateScore] = []
        for i, (cid, text) in enumerate(zip(ids, candidates)):
            others = [c for j, c in enumerate(candidates) if j != i]
            raw_scores = self._score_candidate(text, query, others)

            # Apply custom scorers
            for name, fn in self._custom_scorers.items():
                try:
                    raw_scores[RankCriterion.CUSTOM] = fn(text, query, others)
                except Exception as e:
                    log.warning(f"Custom scorer {name!r} failed: {e}")

            final = self._weighted_score(raw_scores)
            scored.append(
                CandidateScore(
                    candidate_id=cid,
                    text=text,
                    scores=raw_scores,
                    final_score=final,
                    metadata=meta[i],
                )
            )
            self._stats["candidates_scored"] += 1

        scored.sort(key=lambda c: -c.final_score)
        for rank, c in enumerate(scored, 1):
            c.rank = rank

        weights_used = {
            cw.criterion.value: cw.weight for cw in self._weights if cw.enabled
        }

        return RankingResult(
            query=query,
            candidates=scored,
            best=scored[0],
            duration_ms=(time.time() - t0) * 1000,
            weights_used=weights_used,
        )

    def rank_with_scores(
        self,
        query: str,
        candidates: list[dict],  # [{id, text, ...}]
    ) -> RankingResult:
        ids = [c.get("id", f"c{i}") for i, c in enumerate(candidates)]
        texts = [c.get("text", "") for c in candidates]
        meta = [
            {k: v for k, v in c.items() if k not in ("id", "text")} for c in candidates
        ]
        return self.rank(query, texts, ids, meta)

    def best(self, query: str, candidates: list[str]) -> str:
        result = self.rank(query, candidates)
        return result.best.text

    def stats(self) -> dict:
        return {**self._stats, "weights": len(self._weights)}


def build_jarvis_response_ranker() -> ResponseRanker:
    return ResponseRanker()


def main():
    import sys

    ranker = build_jarvis_response_ranker()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Response ranker demo...\n")

        query = "What GPU models are available on the M1 cluster node?"

        candidates = [
            "The M1 node runs RTX 3090, RTX 3080, and RTX 4090 GPUs, "
            "providing 24GB, 10GB, and 24GB VRAM respectively.",
            "I cannot provide information about GPU hardware configurations.",
            "M1 has GPUs. They are used for AI. The cluster is fast. "
            "It can run many models. The system is powerful. "
            "You can use it for inference. It supports many frameworks. "
            "The hardware is modern. Performance is good. We like it.",
            "The M1 node is equipped with RTX 3090 and RTX 4090 graphics cards, "
            "suitable for large language model inference workloads.",
            "Maybe M1 has some GPUs, I think perhaps RTX 3090? Not sure about the others.",
        ]

        result = ranker.rank(query, candidates)

        print(f"  Query: {query[:60]}\n")
        for c in result.candidates:
            print(f"  #{c.rank} [{c.final_score:.3f}] {c.text[:70]}")
            top_scores = sorted(c.scores.items(), key=lambda x: -x[1])[:3]
            print(f"     top: {', '.join(f'{k.value}={v:.2f}' for k, v in top_scores)}")
        print(f"\n  Best: {result.best.text[:80]}")
        print(f"  Duration: {result.duration_ms:.1f}ms")

        print(f"\nStats: {json.dumps(ranker.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(ranker.stats(), indent=2))


if __name__ == "__main__":
    main()
