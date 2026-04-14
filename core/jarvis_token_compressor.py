#!/usr/bin/env python3
"""
jarvis_token_compressor — Token-efficient text compression for LLM prompts
Sentence dedup, stopword pruning, importance scoring, progressive compression
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

log = logging.getLogger("jarvis.token_compressor")

CHARS_PER_TOKEN = 3.8

_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "must",
        "shall",
        "can",
        "need",
        "dare",
        "and",
        "but",
        "or",
        "nor",
        "for",
        "yet",
        "so",
        "although",
        "because",
        "since",
        "while",
        "as",
        "if",
        "when",
        "where",
        "which",
        "that",
        "who",
        "this",
        "these",
        "those",
        "it",
        "its",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "with",
        "by",
        "from",
        "about",
        "into",
        "through",
        "during",
        "before",
        "after",
        "above",
        "below",
        "between",
        "each",
        "than",
        "very",
        "just",
        "also",
        "not",
        "no",
        "only",
        "same",
        "so",
        "then",
        "there",
        "here",
        "any",
        "all",
        "both",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "however",
        "therefore",
        "moreover",
        "furthermore",
    }
)


class CompressMode(str, Enum):
    LIGHT = "light"  # remove obvious filler only (~10-20% reduction)
    MEDIUM = "medium"  # sentence dedup + filler (~20-40% reduction)
    AGGRESSIVE = "aggressive"  # drop low-importance sentences (~40-60% reduction)
    EXTREME = "extreme"  # keywords only (~60-80% reduction)


@dataclass
class SentenceScore:
    text: str
    importance: float  # 0.0 - 1.0
    position: int  # original sentence index
    token_count: int

    def to_dict(self) -> dict:
        return {
            "text": self.text[:80],
            "importance": round(self.importance, 3),
            "position": self.position,
            "tokens": self.token_count,
        }


@dataclass
class CompressionResult:
    original: str
    compressed: str
    mode: CompressMode
    original_tokens: int
    compressed_tokens: int
    duration_ms: float = 0.0
    steps: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ratio(self) -> float:
        return self.compressed_tokens / max(self.original_tokens, 1)

    @property
    def saved_tokens(self) -> int:
        return self.original_tokens - self.compressed_tokens

    def to_dict(self) -> dict:
        return {
            "mode": self.mode.value,
            "original_tokens": self.original_tokens,
            "compressed_tokens": self.compressed_tokens,
            "ratio": round(self.ratio, 3),
            "saved_tokens": self.saved_tokens,
            "steps": self.steps,
            "duration_ms": round(self.duration_ms, 2),
        }


def _est_tokens(text: str) -> int:
    return max(1, int(len(text) / CHARS_PER_TOKEN))


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences, preserving structure."""
    raw = re.split(r"(?<=[.!?])\s+", text)
    sentences = []
    for s in raw:
        s = s.strip()
        if s:
            sentences.append(s)
    return sentences


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().strip())


def _word_freq(sentences: list[str]) -> dict[str, int]:
    freq: dict[str, int] = {}
    for sent in sentences:
        for word in re.findall(r"\b\w+\b", sent.lower()):
            if word not in _STOPWORDS and len(word) > 2:
                freq[word] = freq.get(word, 0) + 1
    return freq


def _score_sentence(
    sent: str,
    freq: dict[str, int],
    position: int,
    total: int,
    max_freq: int,
) -> float:
    """Importance score: TF signal + position bias."""
    words = [
        w
        for w in re.findall(r"\b\w+\b", sent.lower())
        if w not in _STOPWORDS and len(w) > 2
    ]
    if not words:
        return 0.1

    tf_score = sum(freq.get(w, 0) for w in words) / max(max_freq * len(words), 1)

    # Position bias: first and last sentences matter more
    pos_score = 0.5
    if total > 0:
        rel = position / max(total - 1, 1)
        # U-shaped: high at 0 and 1, lower in middle
        pos_score = 0.4 + 0.6 * (1 - 4 * rel * (1 - rel))

    # Length signal: very short sentences are often less informative
    len_score = min(1.0, len(sent) / 100)

    return 0.5 * tf_score + 0.3 * pos_score + 0.2 * len_score


def _remove_filler(text: str) -> tuple[str, list[str]]:
    """Remove filler phrases. Returns (cleaned_text, steps)."""
    steps = []
    patterns = [
        (r"\bAs mentioned (?:earlier|above|before),?\s*", ""),
        (r"\bIt is (?:worth|important to) (?:noting|mention) that\s*", ""),
        (r"\bAs you (?:can see|know|may know),?\s*", ""),
        (r"\bIn other words,?\s*", ""),
        (r"\bTo (?:put it simply|be honest|summarize),?\s*", ""),
        (r"\bWith that (?:said|being said),?\s*", ""),
        (r"\bAt the end of the day,?\s*", ""),
        (r"\bNeedless to say,?\s*", ""),
        (r"\bObviously,?\s*", ""),
        (r"\bOf course,?\s*", ""),
        (r"\bClearly,?\s*", ""),
        (r"\bBasically,?\s*", ""),
        (r"\bEssentially,?\s*", ""),
        (r"\bInterestingly (?:enough)?,?\s*", ""),
    ]
    for pat, repl in patterns:
        new_text = re.sub(pat, repl, text, flags=re.I)
        if new_text != text:
            steps.append(f"filler:{pat[:30]}")
            text = new_text
    # Collapse whitespace
    text = re.sub(r"  +", " ", text).strip()
    return text, steps


def _dedup_sentences(sentences: list[str]) -> tuple[list[str], int]:
    """Remove near-duplicate sentences (normalized equality)."""
    seen: set[str] = set()
    result: list[str] = []
    dropped = 0
    for sent in sentences:
        norm = _normalize(sent)
        if norm not in seen:
            seen.add(norm)
            result.append(sent)
        else:
            dropped += 1
    return result, dropped


def _extract_keywords(text: str, top_n: int = 30) -> str:
    """Extreme compression: keep only top-N keywords."""
    words = re.findall(r"\b\w+\b", text.lower())
    freq: dict[str, int] = {}
    for w in words:
        if w not in _STOPWORDS and len(w) > 2:
            freq[w] = freq.get(w, 0) + 1
    top = sorted(freq, key=lambda w: -freq[w])[:top_n]
    return " ".join(top)


class TokenCompressor:
    def __init__(self, default_mode: CompressMode = CompressMode.MEDIUM):
        self._default_mode = default_mode
        self._stats: dict[str, int | float] = {
            "compressions": 0,
            "total_tokens_in": 0,
            "total_tokens_out": 0,
        }

    def compress(
        self,
        text: str,
        mode: CompressMode | None = None,
        target_tokens: int | None = None,
    ) -> CompressionResult:
        t0 = time.time()
        m = mode or self._default_mode
        if target_tokens:
            m = self._pick_mode_for_target(text, target_tokens)

        original_tokens = _est_tokens(text)
        self._stats["compressions"] += 1
        self._stats["total_tokens_in"] += original_tokens

        if m == CompressMode.LIGHT:
            result_text, steps = self._compress_light(text)
        elif m == CompressMode.MEDIUM:
            result_text, steps = self._compress_medium(text)
        elif m == CompressMode.AGGRESSIVE:
            result_text, steps = self._compress_aggressive(text)
        else:
            result_text, steps = self._compress_extreme(text)

        compressed_tokens = _est_tokens(result_text)
        self._stats["total_tokens_out"] += compressed_tokens

        return CompressionResult(
            original=text,
            compressed=result_text,
            mode=m,
            original_tokens=original_tokens,
            compressed_tokens=compressed_tokens,
            duration_ms=(time.time() - t0) * 1000,
            steps=steps,
        )

    def _pick_mode_for_target(self, text: str, target: int) -> CompressMode:
        orig = _est_tokens(text)
        ratio = target / max(orig, 1)
        if ratio >= 0.85:
            return CompressMode.LIGHT
        elif ratio >= 0.65:
            return CompressMode.MEDIUM
        elif ratio >= 0.45:
            return CompressMode.AGGRESSIVE
        else:
            return CompressMode.EXTREME

    def _compress_light(self, text: str) -> tuple[str, list[str]]:
        text, steps = _remove_filler(text)
        # Remove trailing whitespace per line
        lines = [l.rstrip() for l in text.splitlines()]
        text = "\n".join(lines).strip()
        steps.append("light:trim")
        return text, steps

    def _compress_medium(self, text: str) -> tuple[str, list[str]]:
        text, steps = self._compress_light(text)
        sentences = _split_sentences(text)
        deduped, dropped = _dedup_sentences(sentences)
        if dropped:
            steps.append(f"dedup:{dropped}_removed")
        text = " ".join(deduped)
        steps.append("medium:dedup")
        return text, steps

    def _compress_aggressive(self, text: str) -> tuple[str, list[str]]:
        text, steps = self._compress_medium(text)
        sentences = _split_sentences(text)
        if len(sentences) <= 3:
            return text, steps

        freq = _word_freq(sentences)
        max_freq = max(freq.values(), default=1)
        scored = [
            SentenceScore(
                text=s,
                importance=_score_sentence(s, freq, i, len(sentences), max_freq),
                position=i,
                token_count=_est_tokens(s),
            )
            for i, s in enumerate(sentences)
        ]

        # Keep top 60% by importance
        threshold_idx = max(1, int(len(scored) * 0.6))
        by_importance = sorted(scored, key=lambda s: -s.importance)
        keep_ids = {s.position for s in by_importance[:threshold_idx]}

        # Rebuild in original order
        kept = [s.text for s in scored if s.position in keep_ids]
        dropped_count = len(sentences) - len(kept)
        if dropped_count:
            steps.append(f"aggressive:dropped_{dropped_count}_sentences")
        text = " ".join(kept)
        return text, steps

    def _compress_extreme(self, text: str) -> tuple[str, list[str]]:
        text, steps = self._compress_aggressive(text)
        steps.append("extreme:keywords_only")
        keyword_text = _extract_keywords(text, top_n=40)
        return keyword_text, steps

    def score_sentences(self, text: str) -> list[SentenceScore]:
        sentences = _split_sentences(text)
        freq = _word_freq(sentences)
        max_freq = max(freq.values(), default=1)
        return [
            SentenceScore(
                text=s,
                importance=_score_sentence(s, freq, i, len(sentences), max_freq),
                position=i,
                token_count=_est_tokens(s),
            )
            for i, s in enumerate(sentences)
        ]

    def compress_to_budget(self, text: str, token_budget: int) -> CompressionResult:
        """Try progressively stronger modes until within budget."""
        for mode in CompressMode:
            result = self.compress(text, mode=mode)
            if result.compressed_tokens <= token_budget:
                return result
        # Last resort: truncate
        chars = int(token_budget * CHARS_PER_TOKEN)
        truncated = text[:chars] + "…"
        return CompressionResult(
            original=text,
            compressed=truncated,
            mode=CompressMode.EXTREME,
            original_tokens=_est_tokens(text),
            compressed_tokens=token_budget,
            steps=["truncate:forced"],
        )

    def stats(self) -> dict:
        tin = self._stats["total_tokens_in"]
        tout = self._stats["total_tokens_out"]
        return {
            **self._stats,
            "avg_ratio": round(tout / max(tin, 1), 4),
            "total_saved": tin - tout,
        }


def build_jarvis_token_compressor() -> TokenCompressor:
    return TokenCompressor(default_mode=CompressMode.MEDIUM)


def main():
    import sys

    tc = build_jarvis_token_compressor()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Token compressor demo...\n")

        sample = (
            "As mentioned earlier, the JARVIS cluster is a multi-node GPU system. "
            "Basically, it consists of M1, M2, and OL1 nodes. "
            "Needless to say, the system is designed for high availability. "
            "As you can see, each node runs multiple LLM models. "
            "The cluster manages GPU memory allocation across all nodes. "
            "Clearly, the primary node M1 handles most inference traffic. "
            "M2 provides overflow capacity for high-demand periods. "
            "OL1 is a local Ollama instance for lightweight models. "
            "The cluster manages GPU memory allocation across all nodes. "
            "To summarize, the system provides automated failover and load balancing."
        )

        print(f"  Original: {_est_tokens(sample)} tokens\n")

        for mode in CompressMode:
            r = tc.compress(sample, mode=mode)
            print(
                f"  [{mode.value:12}] {r.compressed_tokens:4} tokens "
                f"({r.ratio:.0%}) steps={r.steps}"
            )

        print()

        # Score sentences
        scores = tc.score_sentences(sample)
        print("  Sentence scores (top 3):")
        for s in sorted(scores, key=lambda x: -x.importance)[:3]:
            print(f"    [{s.importance:.2f}] {s.text[:70]}")

        # Budget-based
        r2 = tc.compress_to_budget(sample, target_tokens=30)
        print(f"\n  compress_to_budget(30): {r2.compressed_tokens} tokens")
        print(f"  Text: {r2.compressed[:100]}")

        print(f"\nStats: {json.dumps(tc.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(tc.stats(), indent=2))


if __name__ == "__main__":
    main()
