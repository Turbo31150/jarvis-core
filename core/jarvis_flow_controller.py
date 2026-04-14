#!/usr/bin/env python3
"""
jarvis_flow_controller — Pipeline flow control with throttling and concurrency gates
Token bucket, semaphore gates, and priority queues for async task pipelines
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.flow_controller")

REDIS_PREFIX = "jarvis:flow:"


class GatePolicy(str, Enum):
    PASS_THROUGH = "pass_through"
    THROTTLE = "throttle"
    GATE = "gate"  # binary open/closed
    PRIORITY = "priority"
    CIRCUIT = "circuit"  # circuit breaker pattern


class FlowState(str, Enum):
    OPEN = "open"  # flowing normally
    THROTTLED = "throttled"
    CLOSED = "closed"  # gate shut
    HALF_OPEN = "half_open"  # circuit breaker testing


@dataclass
class TokenBucket:
    capacity: float
    refill_rate: float  # tokens per second
    _tokens: float = field(init=False)
    _last_refill: float = field(default_factory=time.time, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    def __post_init__(self):
        self._tokens = self.capacity

    async def consume(self, tokens: float = 1.0) -> bool:
        async with self._lock:
            now = time.time()
            elapsed = now - self._last_refill
            self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_rate)
            self._last_refill = now
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    @property
    def available(self) -> float:
        elapsed = time.time() - self._last_refill
        return min(self.capacity, self._tokens + elapsed * self.refill_rate)


@dataclass
class PriorityTask:
    priority: int  # higher = more urgent
    task_id: str
    payload: Any
    submitted_at: float = field(default_factory=time.time)
    future: asyncio.Future = field(
        default_factory=asyncio.get_event_loop().create_future
        if False
        else asyncio.Future,
        init=False,
        repr=False,
    )

    def __post_init__(self):
        self.future = asyncio.get_event_loop().create_future()

    def __lt__(self, other: "PriorityTask") -> bool:
        return self.priority > other.priority  # max-heap


@dataclass
class FlowStats:
    passed: int = 0
    throttled: int = 0
    rejected: int = 0
    queued: int = 0
    errors: int = 0
    avg_wait_ms: float = 0.0


@dataclass
class FlowGate:
    name: str
    policy: GatePolicy
    max_concurrent: int = 10
    tokens_per_request: float = 1.0
    bucket: TokenBucket | None = None
    semaphore: asyncio.Semaphore = field(init=False, repr=False)
    state: FlowState = FlowState.OPEN
    failure_threshold: int = 5  # for circuit breaker
    recovery_timeout_s: float = 30.0
    _failures: int = field(default=0, init=False)
    _opened_at: float = field(default=0.0, init=False)
    stats: FlowStats = field(default_factory=FlowStats)

    def __post_init__(self):
        self.semaphore = asyncio.Semaphore(self.max_concurrent)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "policy": self.policy.value,
            "state": self.state.value,
            "max_concurrent": self.max_concurrent,
            "failures": self._failures,
            "stats": {
                "passed": self.stats.passed,
                "throttled": self.stats.throttled,
                "rejected": self.stats.rejected,
            },
        }


class FlowController:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._gates: dict[str, FlowGate] = {}
        self._priority_queues: dict[str, list[PriorityTask]] = {}
        self._global_stats: dict[str, int] = {
            "total_requests": 0,
            "total_passed": 0,
            "total_rejected": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def add_gate(self, gate: FlowGate):
        self._gates[gate.name] = gate
        if gate.policy == GatePolicy.PRIORITY:
            self._priority_queues[gate.name] = []

    def _check_circuit(self, gate: FlowGate) -> bool:
        """Returns True if request should be allowed through."""
        if gate.state == FlowState.OPEN:
            return True
        elif gate.state == FlowState.CLOSED:
            # Try half-open after recovery_timeout_s
            if time.time() - gate._opened_at > gate.recovery_timeout_s:
                gate.state = FlowState.HALF_OPEN
                log.info(f"Gate '{gate.name}' → HALF_OPEN (testing recovery)")
                return True
            return False
        elif gate.state == FlowState.HALF_OPEN:
            return True
        return True

    def record_success(self, gate_name: str):
        gate = self._gates.get(gate_name)
        if not gate:
            return
        if gate.state == FlowState.HALF_OPEN:
            gate.state = FlowState.OPEN
            gate._failures = 0
            log.info(f"Gate '{gate_name}' → OPEN (recovered)")

    def record_failure(self, gate_name: str):
        gate = self._gates.get(gate_name)
        if not gate or gate.policy != GatePolicy.CIRCUIT:
            return
        gate._failures += 1
        gate.stats.errors += 1
        if gate._failures >= gate.failure_threshold:
            gate.state = FlowState.CLOSED
            gate._opened_at = time.time()
            log.warning(
                f"Gate '{gate_name}' → CLOSED (circuit open, {gate._failures} failures)"
            )

    async def acquire(
        self,
        gate_name: str,
        tokens: float = 1.0,
        priority: int = 5,
        timeout_s: float = 30.0,
    ) -> bool:
        gate = self._gates.get(gate_name)
        if not gate:
            return True

        self._global_stats["total_requests"] += 1

        # Manual gate close
        if gate.state == FlowState.CLOSED and gate.policy != GatePolicy.CIRCUIT:
            gate.stats.rejected += 1
            self._global_stats["total_rejected"] += 1
            return False

        # Circuit breaker check
        if gate.policy == GatePolicy.CIRCUIT:
            if not self._check_circuit(gate):
                gate.stats.rejected += 1
                self._global_stats["total_rejected"] += 1
                return False

        # Token bucket throttle
        if gate.policy in (GatePolicy.THROTTLE,) and gate.bucket:
            allowed = await gate.bucket.consume(tokens * gate.tokens_per_request)
            if not allowed:
                gate.stats.throttled += 1
                gate.state = FlowState.THROTTLED
                return False
            gate.state = FlowState.OPEN

        # Concurrency gate
        try:
            await asyncio.wait_for(gate.semaphore.acquire(), timeout=timeout_s)
        except asyncio.TimeoutError:
            gate.stats.rejected += 1
            self._global_stats["total_rejected"] += 1
            return False

        gate.stats.passed += 1
        self._global_stats["total_passed"] += 1
        return True

    def release(self, gate_name: str):
        gate = self._gates.get(gate_name)
        if gate:
            try:
                gate.semaphore.release()
            except ValueError:
                pass

    def open_gate(self, gate_name: str):
        gate = self._gates.get(gate_name)
        if gate:
            gate.state = FlowState.OPEN
            gate._failures = 0

    def close_gate(self, gate_name: str):
        gate = self._gates.get(gate_name)
        if gate:
            gate.state = FlowState.CLOSED

    def gate_info(self, gate_name: str) -> dict | None:
        gate = self._gates.get(gate_name)
        return gate.to_dict() if gate else None

    def all_gates(self) -> list[dict]:
        return [g.to_dict() for g in self._gates.values()]

    def stats(self) -> dict:
        return {
            **self._global_stats,
            "gates": len(self._gates),
        }


def build_jarvis_flow_controller() -> FlowController:
    fc = FlowController()

    # Inference gate: max 8 concurrent, token bucket 20/s
    fc.add_gate(
        FlowGate(
            name="inference",
            policy=GatePolicy.THROTTLE,
            max_concurrent=8,
            bucket=TokenBucket(capacity=20, refill_rate=20.0),
        )
    )

    # Embedding gate: max 16 concurrent, token bucket 50/s
    fc.add_gate(
        FlowGate(
            name="embedding",
            policy=GatePolicy.THROTTLE,
            max_concurrent=16,
            bucket=TokenBucket(capacity=50, refill_rate=50.0),
        )
    )

    # Trading gate: circuit breaker, 3 failures → open
    fc.add_gate(
        FlowGate(
            name="trading",
            policy=GatePolicy.CIRCUIT,
            max_concurrent=4,
            failure_threshold=3,
            recovery_timeout_s=60.0,
        )
    )

    # Admin gate: manually controlled
    fc.add_gate(
        FlowGate(
            name="admin",
            policy=GatePolicy.GATE,
            max_concurrent=2,
        )
    )

    return fc


async def main():
    import sys

    fc = build_jarvis_flow_controller()
    await fc.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Testing flow gates...")

        # Inference gate
        passed = 0
        for i in range(15):
            ok = await fc.acquire("inference")
            if ok:
                passed += 1
                fc.release("inference")
        print(f"Inference: {passed}/15 passed (token bucket)")

        # Circuit breaker
        print("\nCircuit breaker (trading):")
        for i in range(6):
            ok = await fc.acquire("trading", timeout_s=0.1)
            if ok:
                fc.record_failure("trading")
                fc.release("trading")
                state = fc._gates["trading"].state.value
                print(f"  Request {i + 1}: passed → failure → state={state}")
            else:
                print(f"  Request {i + 1}: REJECTED (circuit open)")

        # Manual gate close
        fc.close_gate("admin")
        ok = await fc.acquire("admin", timeout_s=0.1)
        print(f"\nAdmin gate (closed): {'passed' if ok else 'REJECTED'}")
        fc.open_gate("admin")
        ok = await fc.acquire("admin", timeout_s=0.1)
        if ok:
            fc.release("admin")
        print(f"Admin gate (opened): {'passed' if ok else 'REJECTED'}")

        print("\nGate summary:")
        for g in fc.all_gates():
            print(
                f"  {g['name']:<12} state={g['state']:<10} "
                f"passed={g['stats']['passed']} rejected={g['stats']['rejected']}"
            )

        print(f"\nStats: {json.dumps(fc.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(fc.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
