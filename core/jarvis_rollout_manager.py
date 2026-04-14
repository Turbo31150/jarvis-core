#!/usr/bin/env python3
"""
jarvis_rollout_manager — Progressive model/config rollout with automatic promotion
Manages canary → staged → full rollout with health-gate checks at each phase
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.rollout_manager")

HISTORY_FILE = Path("/home/turbo/IA/Core/jarvis/data/rollout_history.jsonl")
REDIS_PREFIX = "jarvis:rollout:"


class RolloutPhase(str, Enum):
    PENDING = "pending"
    CANARY = "canary"  # 5% traffic
    STAGED = "staged"  # 25% traffic
    MAJORITY = "majority"  # 75% traffic
    FULL = "full"  # 100% traffic
    PAUSED = "paused"
    ROLLED_BACK = "rolled_back"
    COMPLETED = "completed"


class RolloutStrategy(str, Enum):
    TIMED = "timed"  # advance after N seconds
    HEALTH_GATE = "health_gate"  # advance only when health checks pass
    MANUAL = "manual"  # require explicit promote() call
    AUTO = "auto"  # health_gate + timed fallback


# Phase traffic percentages
_PHASE_TRAFFIC: dict[RolloutPhase, float] = {
    RolloutPhase.PENDING: 0.0,
    RolloutPhase.CANARY: 0.05,
    RolloutPhase.STAGED: 0.25,
    RolloutPhase.MAJORITY: 0.75,
    RolloutPhase.FULL: 1.0,
    RolloutPhase.PAUSED: 0.0,
    RolloutPhase.ROLLED_BACK: 0.0,
    RolloutPhase.COMPLETED: 1.0,
}

_PHASE_ORDER = [
    RolloutPhase.PENDING,
    RolloutPhase.CANARY,
    RolloutPhase.STAGED,
    RolloutPhase.MAJORITY,
    RolloutPhase.FULL,
    RolloutPhase.COMPLETED,
]


@dataclass
class HealthGate:
    min_requests: int = 10
    max_error_rate: float = 0.05
    max_latency_p95_ms: float = 3000.0
    observe_window_s: float = 60.0


@dataclass
class PhaseMetrics:
    phase: RolloutPhase
    started_at: float
    requests: int = 0
    errors: int = 0
    _latencies: list[float] = field(default_factory=list, repr=False)

    @property
    def error_rate(self) -> float:
        return self.errors / max(self.requests, 1)

    @property
    def p95_latency_ms(self) -> float:
        if not self._latencies:
            return 0.0
        s = sorted(self._latencies)
        return s[min(int(len(s) * 0.95), len(s) - 1)]

    def record(self, latency_ms: float, error: bool = False):
        self.requests += 1
        if error:
            self.errors += 1
        self._latencies.append(latency_ms)
        if len(self._latencies) > 1000:
            self._latencies.pop(0)

    def passes_gate(self, gate: HealthGate) -> tuple[bool, str]:
        elapsed = time.time() - self.started_at
        if elapsed < gate.observe_window_s and self.requests < gate.min_requests:
            return (
                False,
                f"insufficient data ({self.requests}/{gate.min_requests} req in {elapsed:.0f}s)",
            )
        if self.error_rate > gate.max_error_rate:
            return (
                False,
                f"error_rate={self.error_rate:.2%} > {gate.max_error_rate:.2%}",
            )
        if self.p95_latency_ms > gate.max_latency_p95_ms:
            return (
                False,
                f"p95={self.p95_latency_ms:.0f}ms > {gate.max_latency_p95_ms:.0f}ms",
            )
        return True, "ok"

    def to_dict(self) -> dict:
        return {
            "phase": self.phase.value,
            "started_at": self.started_at,
            "elapsed_s": round(time.time() - self.started_at, 1),
            "requests": self.requests,
            "errors": self.errors,
            "error_rate": round(self.error_rate, 4),
            "p95_latency_ms": round(self.p95_latency_ms, 1),
        }


@dataclass
class Rollout:
    rollout_id: str
    name: str
    description: str
    strategy: RolloutStrategy
    phase: RolloutPhase = RolloutPhase.PENDING
    gate: HealthGate = field(default_factory=HealthGate)
    phase_duration_s: float = 300.0  # for TIMED strategy
    created_at: float = field(default_factory=time.time)
    phase_history: list[dict] = field(default_factory=list)
    current_metrics: PhaseMetrics | None = None
    metadata: dict = field(default_factory=dict)
    _advance_task: asyncio.Task | None = field(default=None, repr=False)

    @property
    def traffic_pct(self) -> float:
        return _PHASE_TRAFFIC.get(self.phase, 0.0)

    def is_terminal(self) -> bool:
        return self.phase in (
            RolloutPhase.COMPLETED,
            RolloutPhase.ROLLED_BACK,
        )

    def to_dict(self) -> dict:
        return {
            "rollout_id": self.rollout_id,
            "name": self.name,
            "phase": self.phase.value,
            "traffic_pct": self.traffic_pct,
            "strategy": self.strategy.value,
            "current_metrics": self.current_metrics.to_dict()
            if self.current_metrics
            else {},
            "phase_history": self.phase_history[-5:],
            "created_at": self.created_at,
        }


class RolloutManager:
    def __init__(self, persist: bool = True):
        self.redis: aioredis.Redis | None = None
        self._rollouts: dict[str, Rollout] = {}
        self._callbacks: dict[str, list[Callable]] = {}  # event → callbacks
        self._persist = persist
        self._stats: dict[str, int] = {
            "created": 0,
            "promoted": 0,
            "rolled_back": 0,
            "completed": 0,
        }
        if persist:
            HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def create(
        self,
        rollout_id: str,
        name: str,
        description: str = "",
        strategy: RolloutStrategy = RolloutStrategy.HEALTH_GATE,
        gate: HealthGate | None = None,
        phase_duration_s: float = 300.0,
        metadata: dict | None = None,
    ) -> Rollout:
        r = Rollout(
            rollout_id=rollout_id,
            name=name,
            description=description,
            strategy=strategy,
            gate=gate or HealthGate(),
            phase_duration_s=phase_duration_s,
            metadata=metadata or {},
        )
        self._rollouts[rollout_id] = r
        self._stats["created"] += 1
        log.info(f"Rollout [{rollout_id}] created: {name}")
        return r

    def start(self, rollout_id: str):
        r = self._rollouts.get(rollout_id)
        if not r:
            raise KeyError(f"Rollout {rollout_id!r} not found")
        self._advance(r, RolloutPhase.CANARY)
        if r.strategy in (RolloutStrategy.TIMED, RolloutStrategy.AUTO):
            r._advance_task = asyncio.create_task(self._timed_loop(r))

    def _advance(self, r: Rollout, new_phase: RolloutPhase):
        old = r.phase
        if r.current_metrics:
            r.phase_history.append(r.current_metrics.to_dict())
        r.phase = new_phase
        r.current_metrics = PhaseMetrics(phase=new_phase, started_at=time.time())
        log.info(
            f"Rollout [{r.rollout_id}] {old.value} → {new_phase.value} ({r.traffic_pct:.0%} traffic)"
        )
        self._fire("phase_change", r, old, new_phase)
        if self._persist:
            self._save_event(r, old, new_phase)
        if self.redis:
            asyncio.create_task(self._redis_update(r))

    def _next_phase(self, current: RolloutPhase) -> RolloutPhase | None:
        try:
            idx = _PHASE_ORDER.index(current)
            return _PHASE_ORDER[idx + 1] if idx + 1 < len(_PHASE_ORDER) else None
        except ValueError:
            return None

    async def _timed_loop(self, r: Rollout):
        while not r.is_terminal():
            await asyncio.sleep(r.phase_duration_s)
            if r.is_terminal() or r.phase == RolloutPhase.PAUSED:
                break
            await self.try_promote(r.rollout_id)

    async def try_promote(self, rollout_id: str) -> tuple[bool, str]:
        r = self._rollouts.get(rollout_id)
        if not r or r.is_terminal():
            return False, "not found or terminal"
        if r.phase == RolloutPhase.PAUSED:
            return False, "paused"

        # Health gate check
        if r.strategy in (RolloutStrategy.HEALTH_GATE, RolloutStrategy.AUTO):
            if r.current_metrics:
                ok, reason = r.current_metrics.passes_gate(r.gate)
                if not ok:
                    log.warning(
                        f"Rollout [{rollout_id}] gate fail at {r.phase.value}: {reason}"
                    )
                    return False, f"gate fail: {reason}"

        next_phase = self._next_phase(r.phase)
        if not next_phase:
            return False, "no next phase"

        self._advance(r, next_phase)
        self._stats["promoted"] += 1
        if next_phase == RolloutPhase.COMPLETED:
            self._stats["completed"] += 1
        return True, next_phase.value

    def promote(self, rollout_id: str):
        """Manual promote (bypasses health gate)."""
        r = self._rollouts.get(rollout_id)
        if not r or r.is_terminal():
            return
        next_phase = self._next_phase(r.phase)
        if next_phase:
            self._advance(r, next_phase)
            self._stats["promoted"] += 1

    def rollback(self, rollout_id: str, reason: str = ""):
        r = self._rollouts.get(rollout_id)
        if not r:
            return
        log.warning(f"Rollout [{rollout_id}] rolling back: {reason}")
        if r._advance_task:
            r._advance_task.cancel()
        self._advance(r, RolloutPhase.ROLLED_BACK)
        self._stats["rolled_back"] += 1

    def pause(self, rollout_id: str):
        r = self._rollouts.get(rollout_id)
        if r:
            r.phase = RolloutPhase.PAUSED

    def resume(self, rollout_id: str):
        r = self._rollouts.get(rollout_id)
        if r and r.phase == RolloutPhase.PAUSED:
            r.phase = RolloutPhase.CANARY  # resume from canary

    def record(self, rollout_id: str, latency_ms: float, error: bool = False):
        r = self._rollouts.get(rollout_id)
        if r and r.current_metrics:
            r.current_metrics.record(latency_ms, error)

    def should_use_new(self, rollout_id: str) -> bool:
        """Returns True if this request should hit the new version."""
        import random

        r = self._rollouts.get(rollout_id)
        if not r:
            return False
        return random.random() < r.traffic_pct

    def on(self, event: str, callback: Callable):
        self._callbacks.setdefault(event, []).append(callback)

    def _fire(self, event: str, *args):
        for cb in self._callbacks.get(event, []):
            try:
                cb(*args)
            except Exception:
                pass

    def _save_event(self, r: Rollout, old: RolloutPhase, new: RolloutPhase):
        try:
            with open(HISTORY_FILE, "a") as f:
                f.write(
                    json.dumps(
                        {
                            "rollout_id": r.rollout_id,
                            "from": old.value,
                            "to": new.value,
                            "ts": time.time(),
                            "traffic_pct": r.traffic_pct,
                        }
                    )
                    + "\n"
                )
        except Exception:
            pass

    async def _redis_update(self, r: Rollout):
        if not self.redis:
            return
        try:
            await self.redis.setex(
                f"{REDIS_PREFIX}{r.rollout_id}",
                3600,
                json.dumps(r.to_dict()),
            )
        except Exception:
            pass

    def list_rollouts(self) -> list[dict]:
        return [r.to_dict() for r in self._rollouts.values()]

    def stats(self) -> dict:
        return {
            **self._stats,
            "active": sum(1 for r in self._rollouts.values() if not r.is_terminal()),
        }


def build_jarvis_rollout_manager() -> RolloutManager:
    return RolloutManager(persist=True)


async def main():
    import sys

    mgr = build_jarvis_rollout_manager()
    await mgr.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Rollout demo...")
        r = mgr.create(
            "qwen35-27b-v2",
            "qwen3.5-27b model upgrade",
            strategy=RolloutStrategy.HEALTH_GATE,
            gate=HealthGate(
                min_requests=3,
                max_error_rate=0.1,
                max_latency_p95_ms=5000,
                observe_window_s=1,
            ),
        )
        mgr.start(r.rollout_id)
        print(f"  Phase: {r.phase.value} ({r.traffic_pct:.0%} traffic)")

        # Record some healthy metrics
        for _ in range(5):
            mgr.record(r.rollout_id, latency_ms=800)

        ok, reason = await mgr.try_promote(r.rollout_id)
        print(f"  Promote → {reason} ({ok}), new phase: {r.phase.value}")

        ok, reason = await mgr.try_promote(r.rollout_id)
        print(f"  Promote → {reason}, new phase: {r.phase.value}")

        print(f"\nRollouts: {[x['phase'] for x in mgr.list_rollouts()]}")
        print(f"Stats: {json.dumps(mgr.stats(), indent=2)}")

    elif cmd == "list":
        for r in mgr.list_rollouts():
            print(json.dumps(r))

    elif cmd == "stats":
        print(json.dumps(mgr.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
