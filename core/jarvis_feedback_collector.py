#!/usr/bin/env python3
"""
jarvis_feedback_collector — Collect and analyze user feedback on LLM responses
Tracks ratings, identifies weak models/prompts, feeds into optimizer
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.feedback_collector")

FEEDBACK_FILE = Path("/home/turbo/IA/Core/jarvis/data/feedback.json")
REDIS_PREFIX = "jarvis:feedback:"
REDIS_INDEX = "jarvis:feedback:index"


@dataclass
class FeedbackEntry:
    feedback_id: str
    session_id: str
    model: str
    backend: str
    prompt_hash: str
    response_preview: str
    rating: int  # 1-5
    comment: str = ""
    tags: list[str] = field(default_factory=list)
    ts: float = field(default_factory=time.time)
    latency_s: float = 0.0

    def to_dict(self) -> dict:
        return {
            "feedback_id": self.feedback_id,
            "session_id": self.session_id,
            "model": self.model,
            "backend": self.backend,
            "prompt_hash": self.prompt_hash,
            "response_preview": self.response_preview[:200],
            "rating": self.rating,
            "comment": self.comment,
            "tags": self.tags,
            "ts": self.ts,
            "latency_s": self.latency_s,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FeedbackEntry":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class FeedbackCollector:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._entries: list[FeedbackEntry] = []
        self._load()

    def _load(self):
        if FEEDBACK_FILE.exists():
            try:
                data = json.loads(FEEDBACK_FILE.read_text())
                self._entries = [
                    FeedbackEntry.from_dict(d) for d in data.get("entries", [])
                ]
            except Exception as e:
                log.warning(f"Load feedback error: {e}")

    def _save(self):
        FEEDBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
        FEEDBACK_FILE.write_text(
            json.dumps(
                {"entries": [e.to_dict() for e in self._entries[-5000:]]},
                indent=2,
            )
        )

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    async def submit(
        self,
        session_id: str,
        model: str,
        backend: str,
        prompt_hash: str,
        response_preview: str,
        rating: int,
        comment: str = "",
        tags: list[str] | None = None,
        latency_s: float = 0.0,
    ) -> FeedbackEntry:
        rating = max(1, min(5, rating))
        entry = FeedbackEntry(
            feedback_id=str(uuid.uuid4())[:8],
            session_id=session_id,
            model=model,
            backend=backend,
            prompt_hash=prompt_hash,
            response_preview=response_preview,
            rating=rating,
            comment=comment,
            tags=tags or [],
            latency_s=latency_s,
        )
        self._entries.append(entry)
        self._save()

        if self.redis:
            await self.redis.lpush(REDIS_INDEX, json.dumps(entry.to_dict()))
            await self.redis.ltrim(REDIS_INDEX, 0, 4999)
            await self.redis.hincrby(f"{REDIS_PREFIX}model:{model}", "total", 1)
            await self.redis.hincrbyfloat(
                f"{REDIS_PREFIX}model:{model}", "rating_sum", rating
            )
            await self.redis.publish(
                "jarvis:events",
                json.dumps(
                    {"event": "feedback_submitted", "rating": rating, "model": model}
                ),
            )

        log.info(f"Feedback [{entry.feedback_id}] model={model} rating={rating}/5")
        return entry

    def model_stats(self, model: str | None = None) -> list[dict]:
        entries = self._entries
        if model:
            entries = [e for e in entries if e.model == model]

        by_model: dict[str, list[int]] = {}
        for e in entries:
            by_model.setdefault(e.model, []).append(e.rating)

        result = []
        for m, ratings in by_model.items():
            result.append(
                {
                    "model": m,
                    "count": len(ratings),
                    "avg_rating": round(mean(ratings), 2),
                    "pct_positive": round(
                        sum(1 for r in ratings if r >= 4) / len(ratings) * 100, 1
                    ),
                    "pct_negative": round(
                        sum(1 for r in ratings if r <= 2) / len(ratings) * 100, 1
                    ),
                }
            )

        return sorted(result, key=lambda x: -x["avg_rating"])

    def tag_analysis(self) -> dict[str, dict]:
        by_tag: dict[str, list[int]] = {}
        for e in self._entries:
            for tag in e.tags:
                by_tag.setdefault(tag, []).append(e.rating)

        return {
            tag: {
                "count": len(ratings),
                "avg_rating": round(mean(ratings), 2),
            }
            for tag, ratings in by_tag.items()
            if len(ratings) >= 3
        }

    def low_rated(self, threshold: int = 2, limit: int = 10) -> list[dict]:
        return [
            e.to_dict()
            for e in sorted(
                [e for e in self._entries if e.rating <= threshold],
                key=lambda x: x.ts,
                reverse=True,
            )[:limit]
        ]

    def recent(self, limit: int = 20) -> list[dict]:
        return [
            e.to_dict()
            for e in sorted(self._entries, key=lambda x: x.ts, reverse=True)[:limit]
        ]

    def summary(self) -> dict:
        if not self._entries:
            return {"total": 0}
        ratings = [e.rating for e in self._entries]
        return {
            "total": len(ratings),
            "avg_rating": round(mean(ratings), 2),
            "rating_distribution": {
                str(r): sum(1 for x in ratings if x == r) for r in range(1, 6)
            },
            "pct_positive": round(
                sum(1 for r in ratings if r >= 4) / len(ratings) * 100, 1
            ),
            "models_tracked": len(set(e.model for e in self._entries)),
        }


async def main():
    import sys

    collector = FeedbackCollector()
    await collector.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "summary"

    if cmd == "summary":
        s = collector.summary()
        print(json.dumps(s, indent=2))

    elif cmd == "models":
        stats = collector.model_stats()
        if not stats:
            print("No feedback data")
        else:
            print(f"{'Model':<40} {'Count':>6} {'Avg':>5} {'+':>6} {'-':>6}")
            print("-" * 65)
            for s in stats:
                print(
                    f"{s['model']:<40} {s['count']:>6} {s['avg_rating']:>5.2f} "
                    f"{s['pct_positive']:>5.1f}% {s['pct_negative']:>5.1f}%"
                )

    elif cmd == "submit" and len(sys.argv) >= 5:
        entry = await collector.submit(
            session_id="cli",
            model=sys.argv[2],
            backend="M1",
            prompt_hash="cli_test",
            response_preview=sys.argv[3],
            rating=int(sys.argv[4]),
            comment=sys.argv[5] if len(sys.argv) > 5 else "",
        )
        print(f"Submitted [{entry.feedback_id}] rating={entry.rating}/5")

    elif cmd == "low":
        items = collector.low_rated()
        for item in items:
            print(
                f"  [{item['rating']}/5] [{item['model']}] {item['response_preview'][:80]}"
            )

    elif cmd == "recent":
        items = collector.recent()
        for item in items:
            ts = time.strftime("%H:%M", time.localtime(item["ts"]))
            print(
                f"  [{ts}] [{item['rating']}/5] {item['model']}: {item['response_preview'][:60]}"
            )

    elif cmd == "tags":
        tags = collector.tag_analysis()
        for tag, info in sorted(tags.items(), key=lambda x: -x[1]["avg_rating"]):
            print(f"  {tag:<20} count={info['count']:>4} avg={info['avg_rating']:.2f}")


if __name__ == "__main__":
    asyncio.run(main())
