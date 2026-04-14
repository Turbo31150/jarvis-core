#!/usr/bin/env python3
"""
jarvis_output_validator — LLM response output validation and quality scoring
Validates structure, detects truncation, scores quality, filters harmful content
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field

log = logging.getLogger("jarvis.output_validator")

# Refusal patterns
REFUSAL_PATTERNS = [
    r"i('m| am) (sorry|unable|not able)",
    r"i (can't|cannot|won't|will not)",
    r"as an (ai|language model)",
    r"i don't have (access|information|the ability)",
    r"that('s| is) (outside|beyond) (my|the)",
]

# Hallucination indicators
HALLUCINATION_PATTERNS = [
    r"as of (my |the )?knowledge cutoff",
    r"i don't have (real-time|current|live)",
    r"based on my training",
    r"i cannot (verify|confirm|access)",
]

# Truncation indicators
TRUNCATION_INDICATORS = [
    "...",
    "…",
    "[continued]",
    "[truncated]",
    "(continued)",
    "to be continued",
    "and so on",
    "etc etc",
]


@dataclass
class QualityScore:
    completeness: float  # 0-1: is response complete?
    relevance: float  # 0-1: does it seem relevant?
    confidence: float  # 0-1: is it confident (not hedging)?
    safety: float  # 0-1: no harmful/refusal content?
    overall: float  # weighted composite

    def to_dict(self) -> dict:
        return {
            "completeness": round(self.completeness, 3),
            "relevance": round(self.relevance, 3),
            "confidence": round(self.confidence, 3),
            "safety": round(self.safety, 3),
            "overall": round(self.overall, 3),
        }


@dataclass
class OutputValidation:
    valid: bool
    finish_reason: str
    content: str
    quality: QualityScore
    issues: list[str] = field(default_factory=list)
    is_refusal: bool = False
    is_truncated: bool = False
    has_hallucination_indicators: bool = False
    word_count: int = 0
    duration_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "valid": self.valid,
            "finish_reason": self.finish_reason,
            "quality": self.quality.to_dict(),
            "issues": self.issues,
            "is_refusal": self.is_refusal,
            "is_truncated": self.is_truncated,
            "has_hallucination_indicators": self.has_hallucination_indicators,
            "word_count": self.word_count,
            "duration_ms": round(self.duration_ms, 1),
        }


class OutputValidator:
    def __init__(
        self,
        min_words: int = 1,
        max_words: int = 50_000,
        require_complete: bool = False,
    ):
        self.min_words = min_words
        self.max_words = max_words
        self.require_complete = require_complete
        self._refusal_re = [re.compile(p, re.IGNORECASE) for p in REFUSAL_PATTERNS]
        self._hallucination_re = [
            re.compile(p, re.IGNORECASE) for p in HALLUCINATION_PATTERNS
        ]
        self._stats: dict[str, int] = {
            "validated": 0,
            "invalid": 0,
            "refusals": 0,
            "truncated": 0,
            "hallucination_flags": 0,
        }

    def _extract_content(self, response: dict) -> tuple[str, str]:
        """Returns (content, finish_reason) from OpenAI-compatible response."""
        choices = response.get("choices", [])
        if not choices:
            return "", "unknown"
        choice = choices[0]
        finish_reason = choice.get("finish_reason") or "unknown"
        msg = choice.get("message", {})
        content = msg.get("content") or choice.get("text", "")
        return content or "", finish_reason

    def _score_completeness(self, content: str, finish_reason: str) -> float:
        if finish_reason == "stop":
            score = 1.0
        elif finish_reason == "length":
            score = 0.5
        else:
            score = 0.7

        # Penalty for truncation indicators
        for ind in TRUNCATION_INDICATORS:
            if content.rstrip().endswith(ind):
                score -= 0.2
                break

        # Penalty for very short responses
        words = len(content.split())
        if words < 3:
            score -= 0.3

        return max(0.0, min(1.0, score))

    def _score_confidence(self, content: str) -> float:
        hedges = [
            r"\bperhaps\b",
            r"\bmaybe\b",
            r"\bi think\b",
            r"\bi believe\b",
            r"\bit seems\b",
            r"\bpossibly\b",
            r"\buncertain\b",
        ]
        hedge_count = sum(1 for h in hedges if re.search(h, content, re.IGNORECASE))
        # Some hedging is ok, excessive hedging is bad
        return max(0.3, 1.0 - hedge_count * 0.1)

    def validate(self, response: dict, prompt: str = "") -> OutputValidation:
        t0 = time.time()
        self._stats["validated"] += 1
        issues = []

        content, finish_reason = self._extract_content(response)
        words = content.split()
        word_count = len(words)

        # Empty response
        if not content.strip():
            self._stats["invalid"] += 1
            return OutputValidation(
                valid=False,
                finish_reason=finish_reason,
                content=content,
                quality=QualityScore(0, 0, 0, 0, 0),
                issues=["Empty response"],
                word_count=0,
                duration_ms=(time.time() - t0) * 1000,
            )

        # Word count bounds
        if word_count < self.min_words:
            issues.append(f"Response too short: {word_count} words < {self.min_words}")
        if word_count > self.max_words:
            issues.append(f"Response too long: {word_count} words > {self.max_words}")

        # Refusal detection
        is_refusal = any(p.search(content) for p in self._refusal_re)
        if is_refusal:
            self._stats["refusals"] += 1
            issues.append("Response appears to be a refusal")

        # Truncation detection
        is_truncated = finish_reason == "length" or any(
            content.rstrip().endswith(ind) for ind in TRUNCATION_INDICATORS
        )
        if is_truncated:
            self._stats["truncated"] += 1
            if self.require_complete:
                issues.append("Response appears truncated (finish_reason=length)")

        # Hallucination indicators
        has_hallucination = any(p.search(content) for p in self._hallucination_re)
        if has_hallucination:
            self._stats["hallucination_flags"] += 1

        # Quality scoring
        completeness = self._score_completeness(content, finish_reason)
        confidence = self._score_confidence(content)
        safety = 0.3 if is_refusal else 1.0
        relevance = 0.8  # Can't judge without semantic comparison

        overall = (
            0.35 * completeness + 0.25 * relevance + 0.2 * confidence + 0.2 * safety
        )

        valid = (
            len([i for i in issues if "too short" not in i and "refusal" not in i]) == 0
        )
        if issues:
            self._stats["invalid"] += 1

        return OutputValidation(
            valid=valid,
            finish_reason=finish_reason,
            content=content,
            quality=QualityScore(completeness, relevance, confidence, safety, overall),
            issues=issues,
            is_refusal=is_refusal,
            is_truncated=is_truncated,
            has_hallucination_indicators=has_hallucination,
            word_count=word_count,
            duration_ms=(time.time() - t0) * 1000,
        )

    def validate_batch(self, responses: list[dict]) -> list[OutputValidation]:
        return [self.validate(r) for r in responses]

    def best_response(self, responses: list[dict]) -> tuple[int, OutputValidation]:
        """Pick best from multiple responses by quality score."""
        validations = self.validate_batch(responses)
        best_idx = max(
            range(len(validations)), key=lambda i: validations[i].quality.overall
        )
        return best_idx, validations[best_idx]

    def stats(self) -> dict:
        total = max(self._stats["validated"], 1)
        return {
            **self._stats,
            "refusal_rate": round(self._stats["refusals"] / total * 100, 1),
            "truncation_rate": round(self._stats["truncated"] / total * 100, 1),
        }


async def main():
    import sys

    validator = OutputValidator(min_words=2)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        responses = [
            {
                "choices": [
                    {
                        "message": {"content": "Paris is the capital of France."},
                        "finish_reason": "stop",
                    }
                ]
            },
            {
                "choices": [
                    {
                        "message": {
                            "content": "I'm sorry, I cannot provide that information as an AI language model."
                        },
                        "finish_reason": "stop",
                    }
                ]
            },
            {
                "choices": [
                    {
                        "message": {"content": "The answer is 42 and the reason is..."},
                        "finish_reason": "length",
                    }
                ]
            },
            {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]},
            {
                "choices": [
                    {
                        "message": {
                            "content": "Based on my training data, I believe perhaps this is correct..."
                        },
                        "finish_reason": "stop",
                    }
                ]
            },
        ]

        labels = ["Normal", "Refusal", "Truncated", "Empty", "Hedgy"]
        for label, resp in zip(labels, responses):
            v = validator.validate(resp)
            q = v.quality
            print(
                f"  {label:<12} valid={str(v.valid):<5} quality={q.overall:.2f} "
                f"refusal={v.is_refusal} trunc={v.is_truncated} words={v.word_count}"
            )
            for issue in v.issues:
                print(f"    ⚠ {issue}")

        best_idx, best = validator.best_response(
            [r for r in responses if r["choices"][0]["message"]["content"]]
        )
        print(f"\nBest response index: {best_idx} (quality={best.quality.overall:.2f})")
        print(f"\nStats: {json.dumps(validator.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(validator.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
