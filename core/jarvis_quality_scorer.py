#!/usr/bin/env python3
"""
jarvis_quality_scorer — LLM response quality scoring: coherence, relevance, safety, fluency
Multi-dimensional scoring with configurable rubrics and aggregate reports
"""

import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

log = logging.getLogger("jarvis.quality_scorer")


class ScoreDimension(str, Enum):
    RELEVANCE = "relevance"  # answer addresses the question
    COHERENCE = "coherence"  # internally consistent, logical
    FLUENCY = "fluency"  # grammatical, natural language
    COMPLETENESS = "completeness"  # covers all aspects of the question
    SAFETY = "safety"  # no harmful content
    CONCISENESS = "conciseness"  # not overly verbose
    FACTUALITY = "factuality"  # factually plausible (heuristic)
    FORMATTING = "formatting"  # appropriate format for context


class ScoreLevel(str, Enum):
    EXCELLENT = "excellent"  # 0.85 – 1.0
    GOOD = "good"  # 0.70 – 0.85
    FAIR = "fair"  # 0.50 – 0.70
    POOR = "poor"  # 0.0  – 0.50


@dataclass
class DimensionScore:
    dimension: ScoreDimension
    score: float  # 0.0 – 1.0
    weight: float = 1.0
    evidence: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "dimension": self.dimension.value,
            "score": round(self.score, 4),
            "weight": self.weight,
            "evidence": self.evidence[:3],
            "flags": self.flags,
        }


@dataclass
class QualityReport:
    response_id: str
    prompt: str
    response: str
    model: str
    dimensions: list[DimensionScore] = field(default_factory=list)
    aggregate_score: float = 0.0
    level: ScoreLevel = ScoreLevel.FAIR
    disqualified: bool = False
    disqualify_reason: str = ""
    ts: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "response_id": self.response_id,
            "model": self.model,
            "aggregate_score": round(self.aggregate_score, 4),
            "level": self.level.value,
            "disqualified": self.disqualified,
            "disqualify_reason": self.disqualify_reason,
            "dimensions": [d.to_dict() for d in self.dimensions],
            "prompt_len": len(self.prompt),
            "response_len": len(self.response),
            "ts": self.ts,
        }


# --- Heuristic scorers (no external model needed) ---

_SAFETY_PATTERNS = [
    r"\b(bomb|weapon|explosive|poison)\b.*\b(make|build|create|synthesize)\b",
    r"\b(hack|exploit|bypass|crack)\b.*\b(password|system|network)\b",
    r"\b(kill|murder|harm)\b.*\b(person|people|someone)\b",
]

_REFUSAL_PHRASES = [
    "i cannot",
    "i can't",
    "i'm unable",
    "i am unable",
    "i won't",
    "i will not",
    "as an ai",
    "i don't have the ability",
]

_HEDGE_PHRASES = [
    "i think",
    "i believe",
    "i'm not sure",
    "possibly",
    "perhaps",
    "it's possible",
    "might be",
    "could be",
]


def _score_safety(response: str) -> DimensionScore:
    text = response.lower()
    flags = []
    score = 1.0

    for pattern in _SAFETY_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            flags.append(f"unsafe pattern: {pattern[:30]}")
            score = 0.0

    return DimensionScore(ScoreDimension.SAFETY, score, weight=2.0, flags=flags)


def _score_fluency(response: str) -> DimensionScore:
    if not response.strip():
        return DimensionScore(ScoreDimension.FLUENCY, 0.0, evidence=["empty response"])

    sentences = re.split(r"[.!?]+", response)
    sentences = [s.strip() for s in sentences if s.strip()]

    if not sentences:
        return DimensionScore(ScoreDimension.FLUENCY, 0.3)

    avg_len = sum(len(s.split()) for s in sentences) / len(sentences)
    # Ideal sentence length: 10-25 words
    if avg_len < 3:
        length_score = 0.4
    elif avg_len > 60:
        length_score = 0.6
    else:
        length_score = 1.0 - abs(avg_len - 17) / 50.0

    # Check for repetition
    words = response.lower().split()
    unique_ratio = len(set(words)) / max(len(words), 1)
    repetition_score = min(1.0, unique_ratio * 1.5)

    score = length_score * 0.6 + repetition_score * 0.4
    return DimensionScore(ScoreDimension.FLUENCY, max(0.0, min(1.0, score)))


def _score_coherence(response: str) -> DimensionScore:
    if len(response) < 20:
        return DimensionScore(
            ScoreDimension.COHERENCE, 0.4, evidence=["very short response"]
        )

    sentences = [s.strip() for s in re.split(r"[.!?]+", response) if s.strip()]
    if len(sentences) < 2:
        return DimensionScore(ScoreDimension.COHERENCE, 0.7)

    # Heuristic: consistent topic (shared vocabulary across sentences)
    all_words = [set(s.lower().split()) for s in sentences]
    if len(all_words) < 2:
        return DimensionScore(ScoreDimension.COHERENCE, 0.8)

    overlaps = []
    for i in range(len(all_words) - 1):
        union = all_words[i] | all_words[i + 1]
        inter = all_words[i] & all_words[i + 1]
        if union:
            overlaps.append(len(inter) / len(union))

    coherence = sum(overlaps) / len(overlaps) if overlaps else 0.5
    # Scale up: even coherent text has low word overlap between sentences
    score = min(1.0, coherence * 3.0 + 0.3)
    return DimensionScore(ScoreDimension.COHERENCE, score)


def _score_relevance(prompt: str, response: str) -> DimensionScore:
    if not prompt or not response:
        return DimensionScore(ScoreDimension.RELEVANCE, 0.0)

    # Check if response is a refusal
    resp_lower = response.lower()
    for phrase in _REFUSAL_PHRASES:
        if phrase in resp_lower:
            return DimensionScore(
                ScoreDimension.RELEVANCE,
                0.3,
                evidence=[f"refusal phrase: {phrase!r}"],
                flags=["refusal"],
            )

    # Simple token overlap between prompt and response
    prompt_tokens = set(re.findall(r"\b\w{3,}\b", prompt.lower()))
    resp_tokens = set(re.findall(r"\b\w{3,}\b", response.lower()))
    if not prompt_tokens:
        return DimensionScore(ScoreDimension.RELEVANCE, 0.7)

    overlap = len(prompt_tokens & resp_tokens) / len(prompt_tokens)
    score = min(1.0, overlap * 2.0 + 0.3)
    return DimensionScore(ScoreDimension.RELEVANCE, score)


def _score_completeness(prompt: str, response: str) -> DimensionScore:
    # Heuristic: longer responses tend to be more complete, up to a point
    words = len(response.split())
    prompt_words = len(prompt.split())
    ratio = words / max(prompt_words * 2, 10)
    score = min(1.0, math.log1p(ratio) / math.log1p(5))
    return DimensionScore(ScoreDimension.COMPLETENESS, score)


def _score_conciseness(response: str) -> DimensionScore:
    words = len(response.split())
    # Penalize very long responses; reward brevity (within reason)
    if words < 5:
        score = 0.3
    elif words <= 150:
        score = 1.0
    elif words <= 400:
        score = 0.8
    elif words <= 800:
        score = 0.6
    else:
        score = max(0.3, 0.6 - (words - 800) / 5000)
    return DimensionScore(ScoreDimension.CONCISENESS, score)


def _score_formatting(response: str, expected_format: str = "any") -> DimensionScore:
    if expected_format == "json":
        try:
            json.loads(response)
            return DimensionScore(
                ScoreDimension.FORMATTING, 1.0, evidence=["valid JSON"]
            )
        except Exception:
            return DimensionScore(
                ScoreDimension.FORMATTING, 0.0, flags=["invalid JSON"]
            )

    if expected_format == "markdown":
        has_headers = bool(re.search(r"^#{1,3}\s", response, re.MULTILINE))
        has_lists = bool(re.search(r"^[-*]\s", response, re.MULTILINE))
        score = (0.5 if has_headers else 0.0) + (0.5 if has_lists else 0.0)
        return DimensionScore(ScoreDimension.FORMATTING, score)

    return DimensionScore(ScoreDimension.FORMATTING, 0.8)


def _level(score: float) -> ScoreLevel:
    if score >= 0.85:
        return ScoreLevel.EXCELLENT
    elif score >= 0.70:
        return ScoreLevel.GOOD
    elif score >= 0.50:
        return ScoreLevel.FAIR
    return ScoreLevel.POOR


class QualityScorer:
    def __init__(self):
        self._weights: dict[ScoreDimension, float] = {
            ScoreDimension.SAFETY: 2.0,
            ScoreDimension.RELEVANCE: 1.5,
            ScoreDimension.COHERENCE: 1.2,
            ScoreDimension.COMPLETENESS: 1.0,
            ScoreDimension.FLUENCY: 1.0,
            ScoreDimension.CONCISENESS: 0.8,
            ScoreDimension.FORMATTING: 0.7,
        }
        self._disqualify_threshold: float = 0.1  # safety score below this → disqualify
        self._reports: list[QualityReport] = []
        self._max_reports = 10_000
        self._stats: dict[str, int] = {
            "scored": 0,
            "disqualified": 0,
            "excellent": 0,
            "good": 0,
            "fair": 0,
            "poor": 0,
        }

    def score(
        self,
        prompt: str,
        response: str,
        model: str = "unknown",
        expected_format: str = "any",
        response_id: str | None = None,
        metadata: dict | None = None,
    ) -> QualityReport:
        import secrets

        rid = response_id or secrets.token_hex(8)

        dimensions = [
            _score_safety(response),
            _score_relevance(prompt, response),
            _score_coherence(response),
            _score_completeness(prompt, response),
            _score_fluency(response),
            _score_conciseness(response),
            _score_formatting(response, expected_format),
        ]

        # Override weights from config
        for d in dimensions:
            d.weight = self._weights.get(d.dimension, 1.0)

        # Weighted aggregate
        total_w = sum(d.weight for d in dimensions)
        aggregate = sum(d.score * d.weight for d in dimensions) / max(total_w, 1.0)

        # Check safety disqualification
        safety_dim = next(
            (d for d in dimensions if d.dimension == ScoreDimension.SAFETY), None
        )
        disqualified = False
        disq_reason = ""
        if safety_dim and safety_dim.score <= self._disqualify_threshold:
            disqualified = True
            disq_reason = f"safety score {safety_dim.score:.2f} ≤ threshold {self._disqualify_threshold}"
            aggregate = 0.0

        report = QualityReport(
            response_id=rid,
            prompt=prompt,
            response=response,
            model=model,
            dimensions=dimensions,
            aggregate_score=aggregate,
            level=_level(aggregate),
            disqualified=disqualified,
            disqualify_reason=disq_reason,
            metadata=metadata or {},
        )

        self._reports.append(report)
        if len(self._reports) > self._max_reports:
            self._reports.pop(0)

        self._stats["scored"] += 1
        if disqualified:
            self._stats["disqualified"] += 1
        self._stats[report.level.value] += 1

        return report

    def score_many(self, pairs: list[dict]) -> list[QualityReport]:
        return [self.score(**p) for p in pairs]

    def model_comparison(self, metric: str = "aggregate_score") -> dict[str, float]:
        by_model: dict[str, list[float]] = {}
        for r in self._reports:
            if r.model not in by_model:
                by_model[r.model] = []
            if metric == "aggregate_score":
                by_model[r.model].append(r.aggregate_score)
        return {m: round(sum(v) / len(v), 4) if v else 0.0 for m, v in by_model.items()}

    def low_quality_responses(
        self, threshold: float = 0.5, limit: int = 20
    ) -> list[dict]:
        bad = [
            r
            for r in self._reports
            if r.aggregate_score < threshold and not r.disqualified
        ]
        return [
            r.to_dict() for r in sorted(bad, key=lambda r: r.aggregate_score)[:limit]
        ]

    def stats(self) -> dict:
        total = self._stats["scored"]
        avg = sum(r.aggregate_score for r in self._reports) / max(total, 1)
        return {
            **self._stats,
            "average_score": round(avg, 4),
            "model_avg": self.model_comparison(),
        }


def build_jarvis_quality_scorer() -> QualityScorer:
    return QualityScorer()


def main():
    import sys

    qs = build_jarvis_quality_scorer()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Quality scorer demo...")

        cases = [
            ("What is 2+2?", "2+2 equals 4.", "qwen3.5-9b"),
            (
                "Explain quantum entanglement.",
                "Quantum entanglement is a phenomenon where two particles become correlated such that the quantum state of each cannot be described independently. When measured, their states are instantaneously correlated regardless of distance.",
                "deepseek-r1",
            ),
            (
                "Write a poem about cats.",
                "Cats cats cats cats cats cats cats cats cats cats cats cats cats.",
                "gemma3",
            ),
            (
                "How do I bake a cake?",
                "I cannot help with that as it may be harmful.",
                "qwen3.5",
            ),
            ("List three planets.", "Mars, Jupiter, Saturn.", "qwen3.5-9b"),
        ]

        for prompt, response, model in cases:
            r = qs.score(prompt, response, model=model)
            icon = {"excellent": "🌟", "good": "✅", "fair": "⚠️", "poor": "❌"}.get(
                r.level.value, "?"
            )
            flags = " ".join(f.flags for f in r.dimensions for _ in f.flags) or ""
            print(
                f"  {icon} [{model:<15}] {r.aggregate_score:.2f} {r.level.value:<10} {prompt[:30]!r}"
            )
            for d in r.dimensions:
                if d.score < 0.6 or d.flags:
                    print(f"      {d.dimension.value:<14} {d.score:.2f} {d.flags}")

        print(f"\nModel comparison: {json.dumps(qs.model_comparison(), indent=2)}")
        print(f"\nStats: {json.dumps(qs.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(qs.stats(), indent=2))


if __name__ == "__main__":
    main()
