#!/usr/bin/env python3
"""
jarvis_decision_log — Immutable audit log of agent decisions and rationale
Tracks why each decision was made, who made it, what alternatives existed
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.decision_log")

LOG_FILE = Path("/home/turbo/IA/Core/jarvis/data/decision_log.jsonl")
REDIS_PREFIX = "jarvis:decisions:"
REDIS_STREAM = "jarvis:decisions:stream"


class DecisionKind(str, Enum):
    MODEL_SELECTION = "model_selection"
    ROUTE_CHOICE = "route_choice"
    FALLBACK = "fallback"
    BUDGET_ENFORCE = "budget_enforce"
    SCALE_UP = "scale_up"
    SCALE_DOWN = "scale_down"
    ALERT_FIRE = "alert_fire"
    TASK_ASSIGN = "task_assign"
    RETRY = "retry"
    ABORT = "abort"
    CONFIG_CHANGE = "config_change"
    CONSENSUS = "consensus"
    CUSTOM = "custom"


class DecisionOutcome(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DEFERRED = "deferred"
    OVERRIDDEN = "overridden"
    PENDING = "pending"


@dataclass
class DecisionOption:
    label: str
    score: float = 0.0
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "score": round(self.score, 4),
            "metadata": self.metadata,
        }


@dataclass
class Decision:
    decision_id: str
    kind: DecisionKind
    agent_id: str
    chosen: str
    rationale: str
    outcome: DecisionOutcome = DecisionOutcome.ACCEPTED
    alternatives: list[DecisionOption] = field(default_factory=list)
    context: dict = field(default_factory=dict)
    session_id: str = ""
    request_id: str = ""
    ts: float = field(default_factory=time.time)
    duration_ms: float = 0.0
    parent_decision_id: str = ""

    def to_dict(self) -> dict:
        return {
            "decision_id": self.decision_id,
            "kind": self.kind.value,
            "agent_id": self.agent_id,
            "chosen": self.chosen,
            "rationale": self.rationale,
            "outcome": self.outcome.value,
            "alternatives": [a.to_dict() for a in self.alternatives],
            "context": self.context,
            "session_id": self.session_id,
            "request_id": self.request_id,
            "ts": self.ts,
            "duration_ms": round(self.duration_ms, 2),
            "parent_decision_id": self.parent_decision_id,
        }


class DecisionLog:
    def __init__(self, max_memory: int = 10_000, persist: bool = True):
        self.redis: aioredis.Redis | None = None
        self._log: list[Decision] = []
        self._max_memory = max_memory
        self._persist = persist
        self._stats: dict[str, int] = {
            "total": 0,
            "by_kind": {},
            "by_outcome": {},
        }
        if persist:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def record(
        self,
        kind: DecisionKind,
        agent_id: str,
        chosen: str,
        rationale: str,
        alternatives: list[DecisionOption] | None = None,
        outcome: DecisionOutcome = DecisionOutcome.ACCEPTED,
        context: dict | None = None,
        session_id: str = "",
        request_id: str = "",
        duration_ms: float = 0.0,
        parent_decision_id: str = "",
    ) -> Decision:
        d = Decision(
            decision_id=str(uuid.uuid4())[:12],
            kind=kind,
            agent_id=agent_id,
            chosen=chosen,
            rationale=rationale,
            outcome=outcome,
            alternatives=alternatives or [],
            context=context or {},
            session_id=session_id,
            request_id=request_id,
            duration_ms=duration_ms,
            parent_decision_id=parent_decision_id,
        )

        self._log.append(d)
        if len(self._log) > self._max_memory:
            self._log.pop(0)

        # Stats
        self._stats["total"] += 1
        self._stats["by_kind"][kind.value] = (
            self._stats["by_kind"].get(kind.value, 0) + 1
        )
        self._stats["by_outcome"][outcome.value] = (
            self._stats["by_outcome"].get(outcome.value, 0) + 1
        )

        if self._persist:
            try:
                with open(LOG_FILE, "a") as f:
                    f.write(json.dumps(d.to_dict()) + "\n")
            except Exception:
                pass

        if self.redis:
            asyncio.create_task(self._redis_push(d))

        log.debug(
            f"Decision [{kind.value}] by {agent_id}: chose '{chosen}' — {rationale[:60]}"
        )
        return d

    async def _redis_push(self, d: Decision):
        if not self.redis:
            return
        try:
            await self.redis.xadd(
                REDIS_STREAM,
                d.to_dict(),
                maxlen=50_000,
            )
            # Also store latest per agent
            await self.redis.setex(
                f"{REDIS_PREFIX}latest:{d.agent_id}",
                3600,
                json.dumps(d.to_dict()),
            )
        except Exception:
            pass

    def query(
        self,
        agent_id: str | None = None,
        kind: DecisionKind | None = None,
        outcome: DecisionOutcome | None = None,
        since_ts: float = 0.0,
        limit: int = 100,
    ) -> list[Decision]:
        results = self._log
        if agent_id:
            results = [d for d in results if d.agent_id == agent_id]
        if kind:
            results = [d for d in results if d.kind == kind]
        if outcome:
            results = [d for d in results if d.outcome == outcome]
        if since_ts > 0:
            results = [d for d in results if d.ts >= since_ts]
        return results[-limit:]

    def decision_chain(self, decision_id: str) -> list[Decision]:
        """Traverse parent chain from a decision."""
        chain = []
        lookup = {d.decision_id: d for d in self._log}
        current = lookup.get(decision_id)
        while current:
            chain.append(current)
            if not current.parent_decision_id:
                break
            current = lookup.get(current.parent_decision_id)
        return chain

    def summary(self, window_s: float = 3600.0) -> dict:
        cutoff = time.time() - window_s
        recent = [d for d in self._log if d.ts >= cutoff]
        by_kind: dict[str, int] = {}
        by_agent: dict[str, int] = {}
        by_outcome: dict[str, int] = {}
        for d in recent:
            by_kind[d.kind.value] = by_kind.get(d.kind.value, 0) + 1
            by_agent[d.agent_id] = by_agent.get(d.agent_id, 0) + 1
            by_outcome[d.outcome.value] = by_outcome.get(d.outcome.value, 0) + 1
        return {
            "window_s": window_s,
            "count": len(recent),
            "by_kind": by_kind,
            "by_agent": by_agent,
            "by_outcome": by_outcome,
        }

    def stats(self) -> dict:
        return {
            **self._stats,
            "memory_size": len(self._log),
        }


def build_jarvis_decision_log() -> DecisionLog:
    return DecisionLog(max_memory=10_000, persist=True)


async def main():
    import sys

    log_store = build_jarvis_decision_log()
    await log_store.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Recording decisions...")

        log_store.record(
            DecisionKind.MODEL_SELECTION,
            "inference-gw",
            "qwen3.5-27b",
            "Lowest latency for context length 8K, score=0.92",
            alternatives=[
                DecisionOption("deepseek-r1", 0.85),
                DecisionOption("qwen3.5-9b", 0.78),
            ],
            context={"context_len": 8192, "task": "code_gen"},
        )

        log_store.record(
            DecisionKind.FALLBACK,
            "inference-gw",
            "ol1/gemma3",
            "M1 returned 503, falling back to OL1",
            outcome=DecisionOutcome.ACCEPTED,
            context={"primary": "m1", "error": "HTTP 503"},
        )

        log_store.record(
            DecisionKind.BUDGET_ENFORCE,
            "budget-tracker",
            "REJECT",
            "trading-agent exceeded hourly token budget (101%)",
            outcome=DecisionOutcome.REJECTED,
            context={"agent": "trading-agent", "usage_pct": 101.2},
        )

        for d in log_store.query(limit=10):
            print(
                f"  [{d.ts:.0f}] {d.kind.value:<20} agent={d.agent_id:<15} "
                f"chose={d.chosen!r:<20} outcome={d.outcome.value}"
            )

        print(f"\nSummary (1h): {json.dumps(log_store.summary(), indent=2)}")
        print(f"Stats: {json.dumps(log_store.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(log_store.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
