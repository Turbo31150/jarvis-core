#!/usr/bin/env python3
"""
jarvis_circuit_monitor — Circuit breaker state monitoring and analytics
Tracks open/closed/half-open transitions, failure rates, recovery times
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.circuit_monitor")

EVENTS_FILE = Path("/home/turbo/IA/Core/jarvis/data/circuit_events.jsonl")
REDIS_PREFIX = "jarvis:circuit:"


class CircuitState(str, Enum):
    CLOSED = "closed"  # Normal — requests flow
    OPEN = "open"  # Tripped — requests blocked
    HALF_OPEN = "half_open"  # Probing — limited requests


class EventKind(str, Enum):
    CALL_SUCCESS = "call_success"
    CALL_FAILURE = "call_failure"
    STATE_CHANGE = "state_change"
    PROBE_SENT = "probe_sent"
    RESET = "reset"


@dataclass
class CircuitEvent:
    circuit_id: str
    kind: EventKind
    state: CircuitState
    ts: float = field(default_factory=time.time)
    error: str = ""
    latency_ms: float = 0.0
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "circuit_id": self.circuit_id,
            "kind": self.kind.value,
            "state": self.state.value,
            "ts": self.ts,
            "error": self.error[:120] if self.error else "",
            "latency_ms": round(self.latency_ms, 2),
        }


@dataclass
class CircuitStats:
    circuit_id: str
    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    total_calls: int = 0
    total_failures: int = 0
    total_successes: int = 0
    last_failure_ts: float = 0.0
    last_success_ts: float = 0.0
    last_state_change_ts: float = field(default_factory=time.time)
    open_count: int = 0  # How many times tripped
    avg_latency_ms: float = 0.0
    _latency_sum: float = field(default=0.0, repr=False)

    def failure_rate(self, window: int = 100) -> float:
        if self.total_calls == 0:
            return 0.0
        n = min(self.total_calls, window)
        # Approximate: use recent consecutive data
        return min(1.0, self.consecutive_failures / max(n, 1))

    def time_in_state_s(self) -> float:
        return time.time() - self.last_state_change_ts

    def to_dict(self) -> dict:
        return {
            "circuit_id": self.circuit_id,
            "state": self.state.value,
            "consecutive_failures": self.consecutive_failures,
            "consecutive_successes": self.consecutive_successes,
            "total_calls": self.total_calls,
            "total_failures": self.total_failures,
            "total_successes": self.total_successes,
            "failure_rate": round(self.failure_rate(), 4),
            "open_count": self.open_count,
            "avg_latency_ms": round(self.avg_latency_ms, 2),
            "time_in_state_s": round(self.time_in_state_s(), 1),
            "last_failure_ts": self.last_failure_ts,
            "last_success_ts": self.last_success_ts,
        }


@dataclass
class CircuitPolicy:
    failure_threshold: int = 5  # consecutive failures → OPEN
    success_threshold: int = 2  # consecutive successes in HALF_OPEN → CLOSED
    recovery_timeout_s: float = 30.0  # OPEN → HALF_OPEN
    half_open_max_calls: int = 1  # allowed calls in HALF_OPEN


class CircuitMonitor:
    def __init__(self, persist: bool = True):
        self.redis: aioredis.Redis | None = None
        self._circuits: dict[str, CircuitStats] = {}
        self._policies: dict[str, CircuitPolicy] = {}
        self._events: list[CircuitEvent] = []
        self._max_events = 10_000
        self._persist = persist
        self._half_open_calls: dict[str, int] = {}
        self._stats: dict[str, int] = {
            "total_events": 0,
            "trips": 0,
            "recoveries": 0,
        }
        if persist:
            EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def register(self, circuit_id: str, policy: CircuitPolicy | None = None):
        if circuit_id not in self._circuits:
            self._circuits[circuit_id] = CircuitStats(circuit_id=circuit_id)
        self._policies[circuit_id] = policy or CircuitPolicy()
        self._half_open_calls[circuit_id] = 0

    def _get_or_create(self, circuit_id: str) -> tuple[CircuitStats, CircuitPolicy]:
        if circuit_id not in self._circuits:
            self.register(circuit_id)
        return self._circuits[circuit_id], self._policies[circuit_id]

    def is_allowed(self, circuit_id: str) -> bool:
        stats, policy = self._get_or_create(circuit_id)

        if stats.state == CircuitState.CLOSED:
            return True

        if stats.state == CircuitState.OPEN:
            # Check if recovery timeout has passed
            if stats.time_in_state_s() >= policy.recovery_timeout_s:
                self._transition(circuit_id, CircuitState.HALF_OPEN)
                self._half_open_calls[circuit_id] = 0
                return True
            return False

        if stats.state == CircuitState.HALF_OPEN:
            current = self._half_open_calls.get(circuit_id, 0)
            if current < policy.half_open_max_calls:
                self._half_open_calls[circuit_id] = current + 1
                return True
            return False

        return True

    def record_success(self, circuit_id: str, latency_ms: float = 0.0):
        stats, policy = self._get_or_create(circuit_id)
        stats.total_calls += 1
        stats.total_successes += 1
        stats.consecutive_successes += 1
        stats.consecutive_failures = 0
        stats.last_success_ts = time.time()
        stats._latency_sum += latency_ms
        stats.avg_latency_ms = stats._latency_sum / stats.total_calls

        # HALF_OPEN → CLOSED if enough successes
        if (
            stats.state == CircuitState.HALF_OPEN
            and stats.consecutive_successes >= policy.success_threshold
        ):
            self._transition(circuit_id, CircuitState.CLOSED)
            self._stats["recoveries"] += 1

        self._push_event(
            CircuitEvent(
                circuit_id, EventKind.CALL_SUCCESS, stats.state, latency_ms=latency_ms
            )
        )

    def record_failure(self, circuit_id: str, error: str = "", latency_ms: float = 0.0):
        stats, policy = self._get_or_create(circuit_id)
        stats.total_calls += 1
        stats.total_failures += 1
        stats.consecutive_failures += 1
        stats.consecutive_successes = 0
        stats.last_failure_ts = time.time()
        stats._latency_sum += latency_ms
        stats.avg_latency_ms = stats._latency_sum / stats.total_calls

        # CLOSED/HALF_OPEN → OPEN if threshold exceeded
        if stats.state in (CircuitState.CLOSED, CircuitState.HALF_OPEN):
            if stats.consecutive_failures >= policy.failure_threshold:
                self._transition(circuit_id, CircuitState.OPEN)
                stats.open_count += 1
                self._stats["trips"] += 1

        self._push_event(
            CircuitEvent(
                circuit_id,
                EventKind.CALL_FAILURE,
                stats.state,
                error=error,
                latency_ms=latency_ms,
            )
        )

    def _transition(self, circuit_id: str, new_state: CircuitState):
        stats = self._circuits[circuit_id]
        old_state = stats.state
        stats.state = new_state
        stats.last_state_change_ts = time.time()
        log.warning(f"Circuit [{circuit_id}] {old_state.value} → {new_state.value}")
        self._push_event(
            CircuitEvent(
                circuit_id,
                EventKind.STATE_CHANGE,
                new_state,
                metadata={"from": old_state.value, "to": new_state.value},
            )
        )

    def reset(self, circuit_id: str):
        if circuit_id in self._circuits:
            stats = self._circuits[circuit_id]
            stats.state = CircuitState.CLOSED
            stats.consecutive_failures = 0
            stats.consecutive_successes = 0
            stats.last_state_change_ts = time.time()
            self._half_open_calls[circuit_id] = 0
            self._push_event(
                CircuitEvent(circuit_id, EventKind.RESET, CircuitState.CLOSED)
            )

    def _push_event(self, evt: CircuitEvent):
        self._events.append(evt)
        if len(self._events) > self._max_events:
            self._events.pop(0)
        self._stats["total_events"] += 1

        if self._persist:
            try:
                with open(EVENTS_FILE, "a") as f:
                    f.write(json.dumps(evt.to_dict()) + "\n")
            except Exception:
                pass

        if self.redis:
            asyncio.create_task(self._redis_update(evt.circuit_id))

    async def _redis_update(self, circuit_id: str):
        if not self.redis or circuit_id not in self._circuits:
            return
        try:
            await self.redis.setex(
                f"{REDIS_PREFIX}{circuit_id}",
                300,
                json.dumps(self._circuits[circuit_id].to_dict()),
            )
        except Exception:
            pass

    def all_states(self) -> list[dict]:
        return [s.to_dict() for s in self._circuits.values()]

    def open_circuits(self) -> list[str]:
        return [
            cid for cid, s in self._circuits.items() if s.state == CircuitState.OPEN
        ]

    def recent_events(
        self, circuit_id: str | None = None, limit: int = 50
    ) -> list[dict]:
        evts = self._events
        if circuit_id:
            evts = [e for e in evts if e.circuit_id == circuit_id]
        return [e.to_dict() for e in evts[-limit:]]

    def stats(self) -> dict:
        return {
            **self._stats,
            "circuits": len(self._circuits),
            "open_circuits": len(self.open_circuits()),
        }


def build_jarvis_circuit_monitor() -> CircuitMonitor:
    mon = CircuitMonitor(persist=True)
    mon.register(
        "m1-lmstudio", CircuitPolicy(failure_threshold=3, recovery_timeout_s=20.0)
    )
    mon.register(
        "m2-lmstudio", CircuitPolicy(failure_threshold=3, recovery_timeout_s=20.0)
    )
    mon.register(
        "ol1-ollama", CircuitPolicy(failure_threshold=5, recovery_timeout_s=30.0)
    )
    mon.register(
        "redis-main", CircuitPolicy(failure_threshold=2, recovery_timeout_s=10.0)
    )
    mon.register(
        "trading-api", CircuitPolicy(failure_threshold=3, recovery_timeout_s=60.0)
    )
    return mon


async def main():
    import sys

    mon = build_jarvis_circuit_monitor()
    await mon.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Simulating circuit breaker events...")

        # Simulate failures on m1
        for i in range(3):
            mon.record_failure(
                "m1-lmstudio", error="connection refused", latency_ms=5000
            )
            print(
                f"  Failure {i + 1}: state={mon._circuits['m1-lmstudio'].state.value}"
            )

        print(f"  Allowed: {mon.is_allowed('m1-lmstudio')}")

        # Simulate recovery
        await asyncio.sleep(0.1)
        mon._circuits["m1-lmstudio"].last_state_change_ts -= 25  # fast-forward
        print(f"  After timeout, allowed: {mon.is_allowed('m1-lmstudio')}")
        mon.record_success("m1-lmstudio", latency_ms=150)
        mon.record_success("m1-lmstudio", latency_ms=120)
        print(f"  After 2 successes: state={mon._circuits['m1-lmstudio'].state.value}")

        print("\nAll circuit states:")
        for s in mon.all_states():
            icon = {"closed": "🟢", "open": "🔴", "half_open": "🟡"}.get(
                s["state"], "?"
            )
            print(
                f"  {icon} {s['circuit_id']:<20} failures={s['consecutive_failures']} open_count={s['open_count']}"
            )

        print(f"\nStats: {json.dumps(mon.stats(), indent=2)}")

    elif cmd == "states":
        for s in mon.all_states():
            print(json.dumps(s))

    elif cmd == "stats":
        print(json.dumps(mon.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
