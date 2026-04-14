#!/usr/bin/env python3
"""
jarvis_chaos_injector — Chaos engineering: fault injection, latency, error simulation
Controlled failure injection for resilience testing of the JARVIS cluster
"""

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

log = logging.getLogger("jarvis.chaos_injector")


class FaultKind(str, Enum):
    LATENCY = "latency"  # add delay
    ERROR = "error"  # raise exception
    TIMEOUT = "timeout"  # simulate timeout
    PARTIAL_FAILURE = "partial"  # fail N% of requests
    RESOURCE_EXHAUSTION = "resource"  # simulate OOM / VRAM full
    NETWORK_PARTITION = "partition"  # simulate network split
    DATA_CORRUPTION = "corruption"  # corrupt response payload


class InjectionMode(str, Enum):
    ALWAYS = "always"
    PROBABILISTIC = "probabilistic"
    SCHEDULED = "scheduled"  # active between start_ts and end_ts
    COUNTER = "counter"  # trigger every N calls


@dataclass
class FaultSpec:
    fault_id: str
    kind: FaultKind
    target: str  # component/service name or "*"
    mode: InjectionMode = InjectionMode.PROBABILISTIC
    probability: float = 0.1  # for PROBABILISTIC mode
    interval: int = 10  # for COUNTER mode (every N calls)
    latency_ms: float = 500.0  # for LATENCY kind
    latency_jitter_ms: float = 100.0
    error_message: str = "chaos: injected error"
    error_type: str = "RuntimeError"
    active: bool = True
    start_ts: float = 0.0  # for SCHEDULED mode
    end_ts: float = 0.0
    tags: list[str] = field(default_factory=list)


@dataclass
class InjectionEvent:
    fault_id: str
    kind: FaultKind
    target: str
    injected_at: float = field(default_factory=time.time)
    latency_added_ms: float = 0.0
    error_raised: str = ""
    call_index: int = 0

    def to_dict(self) -> dict:
        return {
            "fault_id": self.fault_id,
            "kind": self.kind.value,
            "target": self.target,
            "injected_at": self.injected_at,
            "latency_added_ms": round(self.latency_added_ms, 2),
            "error_raised": self.error_raised,
            "call_index": self.call_index,
        }


class ChaosInjector:
    def __init__(self, enabled: bool = True):
        self._enabled = enabled
        self._specs: dict[str, FaultSpec] = {}
        self._call_counters: dict[str, int] = {}  # fault_id → call count
        self._events: list[InjectionEvent] = []
        self._max_events = 5_000
        self._stats: dict[str, int] = {
            "calls_intercepted": 0,
            "faults_injected": 0,
            "latencies_added": 0,
            "errors_raised": 0,
            "timeouts": 0,
        }

    def enable(self):
        self._enabled = True
        log.warning("ChaosInjector ENABLED — faults active")

    def disable(self):
        self._enabled = False
        log.info("ChaosInjector disabled")

    def add_fault(self, spec: FaultSpec):
        self._specs[spec.fault_id] = spec
        self._call_counters[spec.fault_id] = 0
        log.info(
            f"Fault added: {spec.fault_id!r} kind={spec.kind.value} target={spec.target!r}"
        )

    def remove_fault(self, fault_id: str):
        self._specs.pop(fault_id, None)
        self._call_counters.pop(fault_id, None)

    def _should_inject(self, spec: FaultSpec) -> bool:
        if not spec.active:
            return False
        now = time.time()

        if spec.mode == InjectionMode.ALWAYS:
            return True

        elif spec.mode == InjectionMode.PROBABILISTIC:
            return random.random() < spec.probability

        elif spec.mode == InjectionMode.SCHEDULED:
            return spec.start_ts <= now <= spec.end_ts

        elif spec.mode == InjectionMode.COUNTER:
            cnt = self._call_counters.get(spec.fault_id, 0)
            return cnt % spec.interval == 0

        return False

    def _matches_target(self, spec: FaultSpec, target: str) -> bool:
        if spec.target == "*":
            return True
        if spec.target.endswith("*"):
            return target.startswith(spec.target[:-1])
        return spec.target == target

    async def intercept(self, target: str, fn: Callable, *args, **kwargs) -> Any:
        self._stats["calls_intercepted"] += 1

        if not self._enabled:
            coro = fn(*args, **kwargs)
            if asyncio.iscoroutine(coro):
                return await coro
            return coro

        matching = [s for s in self._specs.values() if self._matches_target(s, target)]

        for spec in matching:
            cnt = self._call_counters.get(spec.fault_id, 0) + 1
            self._call_counters[spec.fault_id] = cnt

            if not self._should_inject(spec):
                continue

            event = InjectionEvent(
                fault_id=spec.fault_id,
                kind=spec.kind,
                target=target,
                call_index=cnt,
            )
            self._stats["faults_injected"] += 1

            if spec.kind == FaultKind.LATENCY:
                jitter = random.uniform(-spec.latency_jitter_ms, spec.latency_jitter_ms)
                delay = max(0.0, (spec.latency_ms + jitter) / 1000.0)
                event.latency_added_ms = delay * 1000
                await asyncio.sleep(delay)
                self._stats["latencies_added"] += 1

            elif spec.kind == FaultKind.TIMEOUT:
                self._stats["timeouts"] += 1
                event.error_raised = "TimeoutError"
                self._record(event)
                raise asyncio.TimeoutError(f"chaos: injected timeout for {target!r}")

            elif spec.kind == FaultKind.ERROR:
                self._stats["errors_raised"] += 1
                event.error_raised = spec.error_type
                self._record(event)
                raise RuntimeError(spec.error_message)

            elif spec.kind == FaultKind.PARTIAL_FAILURE:
                if random.random() < spec.probability:
                    self._stats["errors_raised"] += 1
                    event.error_raised = "PartialFailure"
                    self._record(event)
                    raise RuntimeError(f"chaos: partial failure for {target!r}")

            elif spec.kind == FaultKind.RESOURCE_EXHAUSTION:
                event.error_raised = "ResourceExhausted"
                self._record(event)
                raise MemoryError(f"chaos: resource exhausted for {target!r}")

            self._record(event)

        # Execute actual function
        coro = fn(*args, **kwargs)
        if asyncio.iscoroutine(coro):
            return await coro
        return coro

    def wrap(self, target: str, fn: Callable) -> Callable:
        """Return a wrapped version of fn with chaos injection."""

        async def _wrapped(*args, **kwargs):
            return await self.intercept(target, fn, *args, **kwargs)

        return _wrapped

    def _record(self, event: InjectionEvent):
        self._events.append(event)
        if len(self._events) > self._max_events:
            self._events.pop(0)

    def recent_events(self, target: str | None = None, limit: int = 20) -> list[dict]:
        evts = self._events
        if target:
            evts = [e for e in evts if e.target == target]
        return [e.to_dict() for e in evts[-limit:]]

    def list_faults(self) -> list[dict]:
        return [
            {
                "fault_id": s.fault_id,
                "kind": s.kind.value,
                "target": s.target,
                "mode": s.mode.value,
                "probability": s.probability,
                "active": s.active,
            }
            for s in self._specs.values()
        ]

    def stats(self) -> dict:
        return {
            **self._stats,
            "faults_defined": len(self._specs),
            "enabled": self._enabled,
            "injection_rate": round(
                self._stats["faults_injected"]
                / max(self._stats["calls_intercepted"], 1),
                4,
            ),
        }


def build_jarvis_chaos_injector(enabled: bool = False) -> ChaosInjector:
    ci = ChaosInjector(enabled=enabled)

    ci.add_fault(
        FaultSpec(
            fault_id="latency-inference",
            kind=FaultKind.LATENCY,
            target="inference.*",
            mode=InjectionMode.PROBABILISTIC,
            probability=0.05,
            latency_ms=200.0,
            latency_jitter_ms=50.0,
        )
    )
    ci.add_fault(
        FaultSpec(
            fault_id="error-trading",
            kind=FaultKind.ERROR,
            target="trading.execute",
            mode=InjectionMode.COUNTER,
            interval=20,
            error_message="chaos: trading service unavailable",
        )
    )
    ci.add_fault(
        FaultSpec(
            fault_id="timeout-redis",
            kind=FaultKind.TIMEOUT,
            target="redis.*",
            mode=InjectionMode.PROBABILISTIC,
            probability=0.02,
        )
    )

    return ci


async def main():
    import sys

    ci = build_jarvis_chaos_injector(enabled=True)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Chaos injector demo (enabled)...")

        async def mock_inference(prompt: str) -> dict:
            return {"result": f"response to {prompt!r}", "tokens": 50}

        async def mock_trading(symbol: str) -> dict:
            return {"filled": symbol, "price": 50000.0}

        # Run 20 inference calls
        ok = err = timeout = 0
        for i in range(20):
            try:
                r = await ci.intercept(
                    "inference.run", mock_inference, prompt=f"test {i}"
                )
                ok += 1
            except asyncio.TimeoutError:
                timeout += 1
            except Exception:
                err += 1

        print(f"\n  Inference (20 calls): ok={ok} errors={err} timeouts={timeout}")

        # Run 25 trading calls (error every 20)
        ok2 = err2 = 0
        for i in range(25):
            try:
                await ci.intercept("trading.execute", mock_trading, symbol="BTC")
                ok2 += 1
            except Exception:
                err2 += 1

        print(f"  Trading  (25 calls): ok={ok2} errors={err2} (expect ~1)")

        print("\nRecent events:")
        for e in ci.recent_events(limit=5):
            print(f"  {e['kind']:<12} target={e['target']:<25} call={e['call_index']}")

        print(f"\nStats: {json.dumps(ci.stats(), indent=2)}")

    elif cmd == "faults":
        print(json.dumps(ci.list_faults(), indent=2))

    elif cmd == "stats":
        print(json.dumps(ci.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
