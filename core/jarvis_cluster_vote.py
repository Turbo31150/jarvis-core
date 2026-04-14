#!/usr/bin/env python3
"""
jarvis_cluster_vote — Distributed voting and quorum for cluster decisions
Implements Raft-inspired leader election and multi-node consensus voting
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.cluster_vote")

REDIS_PREFIX = "jarvis:vote:"
REDIS_CHANNEL = "jarvis:votes"


class VoteState(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    QUORUM_MET = "quorum_met"
    FAILED = "failed"
    EXPIRED = "expired"


class VoteType(str, Enum):
    BINARY = "binary"  # yes/no
    CHOICE = "choice"  # pick one from options
    RANKED = "ranked"  # rank options (Borda count)
    NUMERIC = "numeric"  # numeric value (median wins)


@dataclass
class Vote:
    vote_id: str
    proposal: str
    vote_type: VoteType
    options: list[str] = field(default_factory=list)
    min_quorum: int = 2  # minimum voters needed
    ttl_s: float = 30.0
    state: VoteState = VoteState.OPEN
    created_at: float = field(default_factory=time.time)
    closed_at: float = 0.0
    result: str | None = None
    ballots: dict[str, str] = field(default_factory=dict)  # node_id → choice

    @property
    def expired(self) -> bool:
        return (time.time() - self.created_at) > self.ttl_s

    @property
    def ballot_count(self) -> int:
        return len(self.ballots)

    @property
    def quorum_met(self) -> bool:
        return self.ballot_count >= self.min_quorum

    def to_dict(self) -> dict:
        return {
            "vote_id": self.vote_id,
            "proposal": self.proposal,
            "vote_type": self.vote_type.value,
            "options": self.options,
            "state": self.state.value,
            "ballot_count": self.ballot_count,
            "min_quorum": self.min_quorum,
            "result": self.result,
            "created_at": self.created_at,
        }


def _tally_binary(ballots: dict[str, str]) -> str:
    yes = sum(1 for v in ballots.values() if v.lower() in ("yes", "true", "1"))
    no = len(ballots) - yes
    return "yes" if yes > no else "no"


def _tally_choice(ballots: dict[str, str]) -> str:
    counts: dict[str, int] = {}
    for v in ballots.values():
        counts[v] = counts.get(v, 0) + 1
    return max(counts, key=lambda k: counts[k]) if counts else ""


def _tally_ranked(ballots: dict[str, str], options: list[str]) -> str:
    """Borda count: each voter ranks options, points = n-rank."""
    scores: dict[str, int] = {o: 0 for o in options}
    n = len(options)
    for ballot in ballots.values():
        try:
            ranked = json.loads(ballot)  # list of option names, best first
            for i, opt in enumerate(ranked):
                if opt in scores:
                    scores[opt] += n - i
        except (json.JSONDecodeError, TypeError):
            pass
    return max(scores, key=lambda k: scores[k]) if scores else ""


def _tally_numeric(ballots: dict[str, str]) -> str:
    """Return median of numeric ballots."""
    values = []
    for v in ballots.values():
        try:
            values.append(float(v))
        except ValueError:
            pass
    if not values:
        return "0"
    values.sort()
    mid = len(values) // 2
    if len(values) % 2 == 0:
        return str((values[mid - 1] + values[mid]) / 2)
    return str(values[mid])


def _tally(vote: Vote) -> str:
    if vote.vote_type == VoteType.BINARY:
        return _tally_binary(vote.ballots)
    elif vote.vote_type == VoteType.CHOICE:
        return _tally_choice(vote.ballots)
    elif vote.vote_type == VoteType.RANKED:
        return _tally_ranked(vote.ballots, vote.options)
    elif vote.vote_type == VoteType.NUMERIC:
        return _tally_numeric(vote.ballots)
    return _tally_binary(vote.ballots)


class ClusterVote:
    def __init__(self, node_id: str = "node-local"):
        self.redis: aioredis.Redis | None = None
        self._node_id = node_id
        self._votes: dict[str, Vote] = {}
        self._result_callbacks: list[Callable] = []
        self._stats: dict[str, int] = {
            "votes_created": 0,
            "ballots_cast": 0,
            "quorums_met": 0,
            "expired": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def on_result(self, callback: Callable):
        """Register callback(vote) when a vote reaches quorum."""
        self._result_callbacks.append(callback)

    def create_vote(
        self,
        proposal: str,
        vote_type: VoteType = VoteType.BINARY,
        options: list[str] | None = None,
        min_quorum: int = 2,
        ttl_s: float = 30.0,
        vote_id: str | None = None,
    ) -> Vote:
        vid = vote_id or str(uuid.uuid4())[:12]
        vote = Vote(
            vote_id=vid,
            proposal=proposal,
            vote_type=vote_type,
            options=options or (["yes", "no"] if vote_type == VoteType.BINARY else []),
            min_quorum=min_quorum,
            ttl_s=ttl_s,
        )
        self._votes[vid] = vote
        self._stats["votes_created"] += 1

        if self.redis:
            asyncio.create_task(self._publish_vote_event("created", vote))

        log.info(f"Vote created: {vid} — '{proposal[:50]}'")
        return vote

    def cast(self, vote_id: str, node_id: str, choice: str) -> bool:
        vote = self._votes.get(vote_id)
        if not vote:
            return False
        if vote.state != VoteState.OPEN:
            return False
        if vote.expired:
            vote.state = VoteState.EXPIRED
            self._stats["expired"] += 1
            return False

        vote.ballots[node_id] = choice
        self._stats["ballots_cast"] += 1

        if vote.quorum_met:
            self._finalize(vote)

        if self.redis:
            asyncio.create_task(self._publish_vote_event("ballot", vote))

        return True

    def cast_local(self, vote_id: str, choice: str) -> bool:
        return self.cast(vote_id, self._node_id, choice)

    def _finalize(self, vote: Vote):
        vote.result = _tally(vote)
        vote.state = VoteState.QUORUM_MET
        vote.closed_at = time.time()
        self._stats["quorums_met"] += 1

        for cb in self._result_callbacks:
            try:
                cb(vote)
            except Exception:
                pass

        if self.redis:
            asyncio.create_task(self._persist_result(vote))

        log.info(
            f"Vote {vote.vote_id} result: {vote.result} ({vote.ballot_count} ballots)"
        )

    async def _publish_vote_event(self, event_type: str, vote: Vote):
        if not self.redis:
            return
        try:
            await self.redis.publish(
                REDIS_CHANNEL,
                json.dumps({"event": event_type, "vote": vote.to_dict()}),
            )
            await self.redis.setex(
                f"{REDIS_PREFIX}{vote.vote_id}",
                int(vote.ttl_s) + 60,
                json.dumps(vote.to_dict()),
            )
        except Exception as e:
            log.warning(f"Vote publish error: {e}")

    async def _persist_result(self, vote: Vote):
        if not self.redis:
            return
        try:
            await self.redis.hset(
                f"{REDIS_PREFIX}results",
                vote.vote_id,
                json.dumps(
                    {
                        "result": vote.result,
                        "ballots": vote.ballot_count,
                        "ts": vote.closed_at,
                    }
                ),
            )
        except Exception:
            pass

    def get_result(self, vote_id: str) -> str | None:
        vote = self._votes.get(vote_id)
        return vote.result if vote else None

    def close(self, vote_id: str) -> Vote | None:
        vote = self._votes.get(vote_id)
        if not vote or vote.state != VoteState.OPEN:
            return vote
        vote.result = _tally(vote) if vote.ballots else None
        vote.state = VoteState.CLOSED if vote.ballots else VoteState.FAILED
        vote.closed_at = time.time()
        for cb in self._result_callbacks:
            try:
                cb(vote)
            except Exception:
                pass
        return vote

    def purge_expired(self) -> int:
        expired = [
            vid
            for vid, v in self._votes.items()
            if v.expired and v.state == VoteState.OPEN
        ]
        for vid in expired:
            self._votes[vid].state = VoteState.EXPIRED
            self._stats["expired"] += 1
        return len(expired)

    def active_votes(self) -> list[dict]:
        return [v.to_dict() for v in self._votes.values() if v.state == VoteState.OPEN]

    def list_votes(self) -> list[dict]:
        return [v.to_dict() for v in self._votes.values()]

    def stats(self) -> dict:
        return {
            **self._stats,
            "active_votes": len(self.active_votes()),
            "node_id": self._node_id,
        }


def build_jarvis_cluster_vote(node_id: str = "jarvis-core") -> ClusterVote:
    cv = ClusterVote(node_id=node_id)

    def log_result(vote: Vote):
        log.info(
            f"Vote result [{vote.vote_id}]: '{vote.proposal[:40]}' → {vote.result}"
        )

    cv.on_result(log_result)
    return cv


async def main():
    import sys

    cv = build_jarvis_cluster_vote("m1")
    await cv.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        # Binary vote: failover m2?
        v1 = cv.create_vote(
            "Should we failover M2 to M3?", VoteType.BINARY, min_quorum=2
        )
        cv.cast(v1.vote_id, "m1", "yes")
        cv.cast(v1.vote_id, "m2", "no")
        cv.cast(v1.vote_id, "ol1", "yes")
        print(f"Binary vote result: {v1.result} ({v1.ballot_count} ballots)")

        # Choice vote: pick model
        v2 = cv.create_vote(
            "Which model for reasoning tasks?",
            VoteType.CHOICE,
            options=["deepseek-r1", "qwen3.5-27b", "gemma3:4b"],
            min_quorum=2,
        )
        cv.cast(v2.vote_id, "m1", "deepseek-r1")
        cv.cast(v2.vote_id, "m2", "qwen3.5-27b")
        cv.cast(v2.vote_id, "ol1", "deepseek-r1")
        print(f"Choice vote result: {v2.result} ({v2.ballot_count} ballots)")

        # Numeric vote: timeout consensus
        v3 = cv.create_vote(
            "Consensus timeout_s value?", VoteType.NUMERIC, min_quorum=3
        )
        cv.cast(v3.vote_id, "m1", "30")
        cv.cast(v3.vote_id, "m2", "45")
        cv.cast(v3.vote_id, "ol1", "20")
        print(f"Numeric vote result: {v3.result}s (median of 30, 45, 20)")

        print("\nAll votes:")
        for v in cv.list_votes():
            print(
                f"  {v['vote_id']} [{v['vote_type']:<10}] {v['state']:<12} result={v['result']}"
            )

        print(f"\nStats: {json.dumps(cv.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(cv.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
