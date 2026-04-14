#!/usr/bin/env python3
"""
jarvis_admission_controller — Request admission control with multi-dimensional gates
Combines rate limits, concurrency caps, resource availability, and priority queuing
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.admission_controller")

REDIS_PREFIX = "jarvis:admission:"


class AdmissionDecision(str, Enum):
    ADMIT = "admit"
    QUEUE = "queue"
    REJECT = "reject"
    SHED = "shed"  # load shedding — reject lowest priority


class RejectReason(str, Enum):
    RATE_LIMIT = "rate_limit"
    CONCURRENCY = "concurrency"
    RESOURCE = "resource"
    QUEUE_FULL = "queue_full"
    LOAD_SHED = "load_shed"
    QUOTA = "quota"
    BLACKLISTED = "blacklisted"


@dataclass
class AdmissionPolicy:
    name: str
    max_rps: float = 100.0  # requests per second
    max_concurrency: int = 50
    max_queue_depth: int = 200
    min_priority: int = 0  # reject below this priority
    load_shed_threshold: float = 0.9  # shed when queue > threshold * max_queue
    token_quota_per_min: int = 0  # 0 = unlimited
    entity_rps: float = 0.0  # per-entity rate limit (0=unlimited)


@dataclass
class AdmissionRequest:
    req_id: str
    entity_id: str
    priority: int = 5  # 0=lowest, 10=highest
    tokens_requested: int = 1000
    model: str = ""
    tags: list[str] = field(default_factory=list)
    ts: float = field(default_factory=time.time)


@dataclass
class AdmissionResult:
    req_id: str
    decision: AdmissionDecision
    reason: RejectReason | None
    queue_position: int = 0
    wait_ms: float = 0.0
    policy_name: str = ""

    def admitted(self) -> bool:
        return self.decision == AdmissionDecision.ADMIT

    def to_dict(self) -> dict:
        return {
            "req_id": self.req_id,
            "decision": self.decision.value,
            "reason": self.reason.value if self.reason else None,
            "queue_position": self.queue_position,
            "wait_ms": round(self.wait_ms, 1),
            "policy": self.policy_name,
        }


class _SlidingWindow:
    def __init__(self, window_s: float = 1.0):
        self._window = window_s
        self._timestamps: list[float] = []

    def count(self) -> int:
        cutoff = time.time() - self._window
        self._timestamps = [t for t in self._timestamps if t >= cutoff]
        return len(self._timestamps)

    def add(self):
        self.count()  # prune first
        self._timestamps.append(time.time())

    def rate(self) -> float:
        return self.count() / self._window


class AdmissionController:
    def __init__(self, policy: AdmissionPolicy | None = None):
        self.redis: aioredis.Redis | None = None
        self._policy = policy or AdmissionPolicy(name="default")
        self._global_rps = _SlidingWindow(1.0)
        self._entity_rps: dict[str, _SlidingWindow] = {}
        self._active: int = 0
        self._queue: list[AdmissionRequest] = []
        self._blacklist: set[str] = set()
        self._token_usage: dict[
            str, list[tuple[float, int]]
        ] = {}  # entity → [(ts, tokens)]
        self._stats: dict[str, int] = {
            "admitted": 0,
            "queued": 0,
            "rejected": 0,
            "shed": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def set_policy(self, policy: AdmissionPolicy):
        self._policy = policy

    def blacklist(self, entity_id: str):
        self._blacklist.add(entity_id)

    def whitelist(self, entity_id: str):
        self._blacklist.discard(entity_id)

    def _check_token_quota(self, entity_id: str, tokens: int) -> bool:
        if not self._policy.token_quota_per_min:
            return True
        cutoff = time.time() - 60.0
        history = [
            (t, tok) for t, tok in self._token_usage.get(entity_id, []) if t >= cutoff
        ]
        self._token_usage[entity_id] = history
        used = sum(tok for _, tok in history)
        return (used + tokens) <= self._policy.token_quota_per_min

    def _record_token_usage(self, entity_id: str, tokens: int):
        if entity_id not in self._token_usage:
            self._token_usage[entity_id] = []
        self._token_usage[entity_id].append((time.time(), tokens))

    def _load_factor(self) -> float:
        return len(self._queue) / max(self._policy.max_queue_depth, 1)

    def evaluate(self, req: AdmissionRequest) -> AdmissionResult:
        p = self._policy

        # Blacklist
        if req.entity_id in self._blacklist:
            self._stats["rejected"] += 1
            return AdmissionResult(
                req.req_id,
                AdmissionDecision.REJECT,
                RejectReason.BLACKLISTED,
                policy_name=p.name,
            )

        # Priority gate
        if req.priority < p.min_priority:
            self._stats["rejected"] += 1
            return AdmissionResult(
                req.req_id,
                AdmissionDecision.REJECT,
                RejectReason.LOAD_SHED,
                policy_name=p.name,
            )

        # Global rate limit
        if self._global_rps.rate() >= p.max_rps:
            # Can we queue?
            if len(self._queue) < p.max_queue_depth:
                self._stats["queued"] += 1
                return AdmissionResult(
                    req.req_id,
                    AdmissionDecision.QUEUE,
                    RejectReason.RATE_LIMIT,
                    queue_position=len(self._queue) + 1,
                    policy_name=p.name,
                )
            else:
                self._stats["rejected"] += 1
                return AdmissionResult(
                    req.req_id,
                    AdmissionDecision.REJECT,
                    RejectReason.QUEUE_FULL,
                    policy_name=p.name,
                )

        # Per-entity rate
        if p.entity_rps > 0:
            if req.entity_id not in self._entity_rps:
                self._entity_rps[req.entity_id] = _SlidingWindow(1.0)
            if self._entity_rps[req.entity_id].rate() >= p.entity_rps:
                self._stats["rejected"] += 1
                return AdmissionResult(
                    req.req_id,
                    AdmissionDecision.REJECT,
                    RejectReason.RATE_LIMIT,
                    policy_name=p.name,
                )

        # Concurrency
        if self._active >= p.max_concurrency:
            if len(self._queue) < p.max_queue_depth:
                self._stats["queued"] += 1
                return AdmissionResult(
                    req.req_id,
                    AdmissionDecision.QUEUE,
                    RejectReason.CONCURRENCY,
                    queue_position=len(self._queue) + 1,
                    policy_name=p.name,
                )
            else:
                self._stats["rejected"] += 1
                return AdmissionResult(
                    req.req_id,
                    AdmissionDecision.REJECT,
                    RejectReason.QUEUE_FULL,
                    policy_name=p.name,
                )

        # Load shedding
        lf = self._load_factor()
        if lf >= p.load_shed_threshold and req.priority < 5:
            self._stats["shed"] += 1
            return AdmissionResult(
                req.req_id,
                AdmissionDecision.SHED,
                RejectReason.LOAD_SHED,
                policy_name=p.name,
            )

        # Token quota
        if not self._check_token_quota(req.entity_id, req.tokens_requested):
            self._stats["rejected"] += 1
            return AdmissionResult(
                req.req_id,
                AdmissionDecision.REJECT,
                RejectReason.QUOTA,
                policy_name=p.name,
            )

        # Admit
        self._global_rps.add()
        if req.entity_id in self._entity_rps:
            self._entity_rps[req.entity_id].add()
        self._record_token_usage(req.entity_id, req.tokens_requested)
        self._active += 1
        self._stats["admitted"] += 1
        return AdmissionResult(
            req.req_id, AdmissionDecision.ADMIT, None, policy_name=p.name
        )

    def release(self, req_id: str):
        """Call when request finishes."""
        if self._active > 0:
            self._active -= 1

    def queue_snapshot(self) -> list[dict]:
        return [
            {"req_id": r.req_id, "priority": r.priority, "entity": r.entity_id}
            for r in self._queue
        ]

    def stats(self) -> dict:
        return {
            **self._stats,
            "active": self._active,
            "queue_depth": len(self._queue),
            "global_rps": round(self._global_rps.rate(), 2),
            "load_factor": round(self._load_factor(), 3),
            "policy": self._policy.name,
        }


def build_jarvis_admission() -> AdmissionController:
    policy = AdmissionPolicy(
        name="jarvis_default",
        max_rps=50.0,
        max_concurrency=20,
        max_queue_depth=100,
        min_priority=1,
        load_shed_threshold=0.85,
        token_quota_per_min=500_000,
        entity_rps=10.0,
    )
    return AdmissionController(policy)


async def main():
    import sys

    ctrl = build_jarvis_admission()
    await ctrl.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        import random

        random.seed(42)
        results: dict[str, int] = {"admit": 0, "queue": 0, "reject": 0, "shed": 0}

        for i in range(80):
            req = AdmissionRequest(
                req_id=f"r{i}",
                entity_id=f"user_{i % 5}",
                priority=random.randint(1, 10),
                tokens_requested=random.randint(100, 5000),
            )
            result = ctrl.evaluate(req)
            results[result.decision.value] = results.get(result.decision.value, 0) + 1
            if result.admitted():
                ctrl.release(req.req_id)

        print("Admission decisions:")
        for k, v in results.items():
            print(f"  {k:<10}: {v}")
        print(f"\nStats: {json.dumps(ctrl.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(ctrl.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
