#!/usr/bin/env python3
"""
jarvis_feedback_engine — Collect, store and aggregate user feedback on LLM responses
Thumbs up/down, ratings, corrections, preference pairs, RLHF-ready export
"""

import json
import logging
import statistics
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.feedback_engine")

REDIS_PREFIX = "jarvis:feedback:"


class FeedbackType(str, Enum):
    THUMBS_UP = "thumbs_up"
    THUMBS_DOWN = "thumbs_down"
    RATING = "rating"  # 1-5 scale
    CORRECTION = "correction"  # user provides better response
    PREFERENCE = "preference"  # A/B preference pair
    FLAG = "flag"  # flag for review
    COMMENT = "comment"  # free-text comment


class FeedbackSignal(str, Enum):
    HELPFUL = "helpful"
    ACCURATE = "accurate"
    SAFE = "safe"
    CONCISE = "concise"
    CLEAR = "clear"
    HARMFUL = "harmful"
    INCORRECT = "incorrect"
    INCOMPLETE = "incomplete"
    VERBOSE = "verbose"


@dataclass
class FeedbackEntry:
    feedback_id: str
    session_id: str
    request_id: str
    model: str
    prompt: str
    response: str
    feedback_type: FeedbackType
    rating: float = 0.0  # 0-5; filled for RATING type
    signals: list[FeedbackSignal] = field(default_factory=list)
    correction: str = ""  # for CORRECTION type
    preferred_id: str = ""  # for PREFERENCE type (id of preferred response)
    comment: str = ""
    user_id: str = ""
    ts: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_positive(self) -> bool:
        if self.feedback_type == FeedbackType.THUMBS_UP:
            return True
        if self.feedback_type == FeedbackType.THUMBS_DOWN:
            return False
        if self.feedback_type == FeedbackType.RATING:
            return self.rating >= 3.5
        return True

    def to_dict(self) -> dict:
        return {
            "feedback_id": self.feedback_id,
            "session_id": self.session_id,
            "request_id": self.request_id,
            "model": self.model,
            "type": self.feedback_type.value,
            "rating": self.rating,
            "signals": [s.value for s in self.signals],
            "correction": self.correction[:200] if self.correction else "",
            "comment": self.comment[:200] if self.comment else "",
            "positive": self.is_positive,
            "ts": self.ts,
        }

    def to_rlhf_dict(self) -> dict:
        """RLHF-ready format for preference learning."""
        return {
            "prompt": self.prompt,
            "chosen": self.correction if self.correction else self.response,
            "rejected": self.response if self.correction else "",
            "model": self.model,
            "score": self.rating / 5.0
            if self.rating
            else (1.0 if self.is_positive else 0.0),
            "signals": [s.value for s in self.signals],
        }


@dataclass
class ModelFeedbackSummary:
    model: str
    total: int
    positive: int
    negative: int
    avg_rating: float
    rating_stddev: float
    signal_counts: dict[str, int]
    correction_rate: float
    flag_rate: float
    recent_trend: float  # +1 = improving, -1 = degrading

    @property
    def satisfaction_rate(self) -> float:
        return self.positive / max(self.total, 1)

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "total_feedback": self.total,
            "satisfaction_rate": round(self.satisfaction_rate, 4),
            "avg_rating": round(self.avg_rating, 3),
            "rating_stddev": round(self.rating_stddev, 3),
            "positive": self.positive,
            "negative": self.negative,
            "correction_rate": round(self.correction_rate, 4),
            "flag_rate": round(self.flag_rate, 4),
            "top_signals": sorted(self.signal_counts.items(), key=lambda x: -x[1])[:5],
            "recent_trend": round(self.recent_trend, 3),
        }


class FeedbackEngine:
    def __init__(self, max_entries: int = 50_000):
        self.redis: aioredis.Redis | None = None
        self._entries: list[FeedbackEntry] = []
        self._max_entries = max_entries
        self._stats: dict[str, int] = {
            "submitted": 0,
            "positive": 0,
            "negative": 0,
            "corrections": 0,
            "flags": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def submit(self, entry: FeedbackEntry) -> str:
        import secrets

        if not entry.feedback_id:
            entry.feedback_id = secrets.token_hex(8)

        self._entries.append(entry)
        if len(self._entries) > self._max_entries:
            self._entries.pop(0)

        self._stats["submitted"] += 1
        if entry.is_positive:
            self._stats["positive"] += 1
        else:
            self._stats["negative"] += 1
        if entry.feedback_type == FeedbackType.CORRECTION:
            self._stats["corrections"] += 1
        if entry.feedback_type == FeedbackType.FLAG:
            self._stats["flags"] += 1

        log.debug(
            f"Feedback [{entry.feedback_id}] "
            f"model={entry.model} type={entry.feedback_type.value}"
        )
        return entry.feedback_id

    def thumbs_up(
        self,
        session_id: str,
        request_id: str,
        model: str,
        prompt: str,
        response: str,
        signals: list[FeedbackSignal] | None = None,
        user_id: str = "",
    ) -> str:
        entry = FeedbackEntry(
            feedback_id="",
            session_id=session_id,
            request_id=request_id,
            model=model,
            prompt=prompt,
            response=response,
            feedback_type=FeedbackType.THUMBS_UP,
            signals=signals or [],
            user_id=user_id,
        )
        return self.submit(entry)

    def thumbs_down(
        self,
        session_id: str,
        request_id: str,
        model: str,
        prompt: str,
        response: str,
        signals: list[FeedbackSignal] | None = None,
        correction: str = "",
        user_id: str = "",
    ) -> str:
        entry = FeedbackEntry(
            feedback_id="",
            session_id=session_id,
            request_id=request_id,
            model=model,
            prompt=prompt,
            response=response,
            feedback_type=FeedbackType.THUMBS_DOWN,
            signals=signals or [],
            correction=correction,
            user_id=user_id,
        )
        return self.submit(entry)

    def rate(
        self,
        session_id: str,
        request_id: str,
        model: str,
        prompt: str,
        response: str,
        rating: float,
        comment: str = "",
        user_id: str = "",
    ) -> str:
        rating = max(1.0, min(5.0, rating))
        entry = FeedbackEntry(
            feedback_id="",
            session_id=session_id,
            request_id=request_id,
            model=model,
            prompt=prompt,
            response=response,
            feedback_type=FeedbackType.RATING,
            rating=rating,
            comment=comment,
            user_id=user_id,
        )
        return self.submit(entry)

    def model_summary(self, model: str) -> ModelFeedbackSummary:
        entries = [e for e in self._entries if e.model == model]
        if not entries:
            return ModelFeedbackSummary(
                model=model,
                total=0,
                positive=0,
                negative=0,
                avg_rating=0.0,
                rating_stddev=0.0,
                signal_counts={},
                correction_rate=0.0,
                flag_rate=0.0,
                recent_trend=0.0,
            )

        positive = sum(1 for e in entries if e.is_positive)
        ratings = [
            e.rating
            for e in entries
            if e.feedback_type == FeedbackType.RATING and e.rating > 0
        ]
        avg_rating = sum(ratings) / len(ratings) if ratings else 0.0
        std_rating = statistics.stdev(ratings) if len(ratings) > 1 else 0.0

        signal_counts: dict[str, int] = {}
        for e in entries:
            for s in e.signals:
                signal_counts[s.value] = signal_counts.get(s.value, 0) + 1

        corrections = sum(
            1 for e in entries if e.feedback_type == FeedbackType.CORRECTION
        )
        flags = sum(1 for e in entries if e.feedback_type == FeedbackType.FLAG)

        # Trend: compare first half vs second half satisfaction
        half = max(len(entries) // 2, 1)
        first_half = entries[:half]
        second_half = entries[half:]
        rate_first = sum(1 for e in first_half if e.is_positive) / len(first_half)
        rate_second = sum(1 for e in second_half if e.is_positive) / len(second_half)
        trend = rate_second - rate_first

        return ModelFeedbackSummary(
            model=model,
            total=len(entries),
            positive=positive,
            negative=len(entries) - positive,
            avg_rating=avg_rating,
            rating_stddev=std_rating,
            signal_counts=signal_counts,
            correction_rate=corrections / len(entries),
            flag_rate=flags / len(entries),
            recent_trend=trend,
        )

    def all_summaries(self) -> list[ModelFeedbackSummary]:
        models = {e.model for e in self._entries}
        return sorted(
            [self.model_summary(m) for m in models],
            key=lambda s: -s.satisfaction_rate,
        )

    def export_rlhf(
        self,
        model: str | None = None,
        min_rating: float = 0.0,
        limit: int = 1000,
    ) -> list[dict]:
        entries = self._entries
        if model:
            entries = [e for e in entries if e.model == model]
        entries = [
            e
            for e in entries
            if e.feedback_type
            in (
                FeedbackType.CORRECTION,
                FeedbackType.RATING,
                FeedbackType.THUMBS_UP,
                FeedbackType.THUMBS_DOWN,
            )
            and (e.rating == 0 or e.rating >= min_rating)
        ]
        return [e.to_rlhf_dict() for e in entries[-limit:]]

    def recent(self, limit: int = 20, model: str | None = None) -> list[dict]:
        entries = self._entries
        if model:
            entries = [e for e in entries if e.model == model]
        return [e.to_dict() for e in entries[-limit:]]

    def flagged(self) -> list[dict]:
        return [
            e.to_dict() for e in self._entries if e.feedback_type == FeedbackType.FLAG
        ]

    def stats(self) -> dict:
        return {
            **self._stats,
            "total_entries": len(self._entries),
            "satisfaction_rate": round(
                self._stats["positive"] / max(self._stats["submitted"], 1), 4
            ),
        }


def build_jarvis_feedback_engine() -> FeedbackEngine:
    return FeedbackEngine(max_entries=50_000)


def main():
    import sys

    engine = build_jarvis_feedback_engine()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Feedback engine demo...\n")

        prompt = "What GPU is on M1?"
        response = "M1 has RTX 3090 and RTX 4090 GPUs."

        engine.thumbs_up(
            "sess_01",
            "req_01",
            "qwen3.5-9b",
            prompt,
            response,
            signals=[FeedbackSignal.ACCURATE, FeedbackSignal.CONCISE],
        )
        engine.thumbs_down(
            "sess_02",
            "req_02",
            "qwen3.5-9b",
            prompt,
            "I don't know.",
            signals=[FeedbackSignal.INCOMPLETE],
            correction=response,
        )
        engine.rate("sess_03", "req_03", "deepseek-r1", prompt, response, 4.5)
        engine.rate(
            "sess_04",
            "req_04",
            "deepseek-r1",
            prompt,
            "Maybe some GPU.",
            2.0,
            comment="Too vague",
        )
        engine.rate("sess_05", "req_05", "qwen3.5-9b", prompt, response, 5.0)

        print("  Summaries:")
        for s in engine.all_summaries():
            print(
                f"  {s.model:<20} sat={s.satisfaction_rate:.0%} "
                f"avg_rating={s.avg_rating:.1f} "
                f"trend={s.recent_trend:+.2f}"
            )

        print(f"\n  RLHF export ({len(engine.export_rlhf())} items):")
        for item in engine.export_rlhf()[:2]:
            print(f"    score={item['score']:.2f} signals={item['signals']}")

        print(f"\nStats: {json.dumps(engine.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(engine.stats(), indent=2))


if __name__ == "__main__":
    main()
