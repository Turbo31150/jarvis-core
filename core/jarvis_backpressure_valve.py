#!/usr/bin/env python3
"""
jarvis_backpressure_valve — Adaptive backpressure control for async pipelines
Monitors queue depth, CPU/memory pressure, and applies flow control signals
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.backpressure_valve")

REDIS_PREFIX = "jarvis:backpressure:"


class PressureLevel(str, Enum):
    GREEN = "green"  # normal operation
    YELLOW = "yellow"  # moderate pressure — throttle
    ORANGE = "orange"  # high pressure — shed load
    RED = "red"  # critical — reject all new work


class ValveAction(str, Enum):
    ACCEPT = "accept"
    THROTTLE = "throttle"  # accept but add delay
    QUEUE = "queue"  # defer to backlog
    REJECT = "reject"  # refuse immediately
    DRAIN = "drain"  # pause intake, drain existing


@dataclass
class PressureSignal:
    source: str  # e.g. "queue_depth", "cpu", "memory", "gpu_vram"
    value: float  # current measured value
    threshold_yellow: float
    threshold_orange: float
    threshold_red: float
    weight: float = 1.0  # contribution to composite pressure

    @property
    def level(self) -> PressureLevel:
        if self.value >= self.threshold_red:
            return PressureLevel.RED
        elif self.value >= self.threshold_orange:
            return PressureLevel.ORANGE
        elif self.value >= self.threshold_yellow:
            return PressureLevel.YELLOW
        return PressureLevel.GREEN

    @property
    def normalized(self) -> float:
        """0.0 = no pressure, 1.0 = red threshold."""
        return min(self.value / max(self.threshold_red, 1e-9), 1.0)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "value": round(self.value, 2),
            "level": self.level.value,
            "normalized": round(self.normalized, 3),
        }


@dataclass
class ValveDecision:
    action: ValveAction
    level: PressureLevel
    composite_pressure: float  # 0.0–1.0
    throttle_ms: float = 0.0  # suggested delay before accepting
    reason: str = ""

    @property
    def accepted(self) -> bool:
        return self.action in (
            ValveAction.ACCEPT,
            ValveAction.THROTTLE,
            ValveAction.QUEUE,
        )

    def to_dict(self) -> dict:
        return {
            "action": self.action.value,
            "level": self.level.value,
            "composite_pressure": round(self.composite_pressure, 3),
            "throttle_ms": round(self.throttle_ms, 1),
            "reason": self.reason,
        }


_LEVEL_PRIORITY = {
    PressureLevel.GREEN: 0,
    PressureLevel.YELLOW: 1,
    PressureLevel.ORANGE: 2,
    PressureLevel.RED: 3,
}


class BackpressureValve:
    def __init__(
        self,
        max_queue_depth: int = 500,
        drain_timeout_s: float = 30.0,
    ):
        self.redis: aioredis.Redis | None = None
        self._signals: dict[str, PressureSignal] = {}
        self._queue_depths: dict[str, int] = {}  # pipeline_id → depth
        self._callbacks: list[Callable] = []  # called on level change
        self._current_level = PressureLevel.GREEN
        self._max_queue = max_queue_depth
        self._drain_timeout = drain_timeout_s
        self._draining = False
        self._throttle_base_ms = 50.0
        self._stats: dict[str, int] = {
            "accepted": 0,
            "throttled": 0,
            "queued": 0,
            "rejected": 0,
            "level_changes": 0,
        }
        self._level_history: list[tuple[float, PressureLevel]] = []

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def register_signal(self, signal: PressureSignal):
        self._signals[signal.source] = signal

    def update_signal(self, source: str, value: float):
        if source in self._signals:
            self._signals[source].value = value

    def set_queue_depth(self, pipeline_id: str, depth: int):
        self._queue_depths[pipeline_id] = depth
        # Update queue pressure signal
        total_depth = sum(self._queue_depths.values())
        if "queue_depth" in self._signals:
            self._signals["queue_depth"].value = total_depth

    def on_level_change(self, callback: Callable):
        self._callbacks.append(callback)

    def _composite_pressure(self) -> tuple[float, PressureLevel]:
        if not self._signals:
            return 0.0, PressureLevel.GREEN
        total_weight = sum(s.weight for s in self._signals.values())
        weighted_pressure = sum(s.normalized * s.weight for s in self._signals.values())
        composite = weighted_pressure / max(total_weight, 1e-9)
        # Worst-case level wins (not average)
        worst = max(
            self._signals.values(),
            key=lambda s: _LEVEL_PRIORITY[s.level],
        )
        return composite, worst.level

    def _notify_level_change(self, old: PressureLevel, new: PressureLevel):
        self._stats["level_changes"] += 1
        self._level_history.append((time.time(), new))
        if len(self._level_history) > 100:
            self._level_history.pop(0)
        log.warning(f"Backpressure level: {old.value} → {new.value}")
        for cb in self._callbacks:
            try:
                cb(old, new)
            except Exception:
                pass
        if self.redis:
            asyncio.create_task(
                self.redis.setex(
                    f"{REDIS_PREFIX}level",
                    300,
                    json.dumps({"level": new.value, "ts": time.time()}),
                )
            )

    def check(self, pipeline_id: str = "") -> ValveDecision:
        composite, level = self._composite_pressure()

        if level != self._current_level:
            self._notify_level_change(self._current_level, level)
            self._current_level = level

        if self._draining:
            decision = ValveDecision(
                ValveAction.DRAIN, level, composite, reason="draining"
            )
            self._stats["rejected"] += 1
            return decision

        if level == PressureLevel.GREEN:
            action = ValveAction.ACCEPT
            throttle_ms = 0.0
        elif level == PressureLevel.YELLOW:
            action = ValveAction.THROTTLE
            throttle_ms = self._throttle_base_ms
        elif level == PressureLevel.ORANGE:
            action = ValveAction.THROTTLE
            throttle_ms = self._throttle_base_ms * 5
        else:  # RED
            action = ValveAction.REJECT
            throttle_ms = 0.0

        decision = ValveDecision(
            action=action,
            level=level,
            composite_pressure=composite,
            throttle_ms=throttle_ms,
            reason=f"composite={composite:.3f}",
        )

        if action == ValveAction.ACCEPT:
            self._stats["accepted"] += 1
        elif action == ValveAction.THROTTLE:
            self._stats["throttled"] += 1
        elif action == ValveAction.REJECT:
            self._stats["rejected"] += 1

        return decision

    async def check_async(self, pipeline_id: str = "") -> ValveDecision:
        decision = self.check(pipeline_id)
        if decision.action == ValveAction.THROTTLE and decision.throttle_ms > 0:
            await asyncio.sleep(decision.throttle_ms / 1000)
        return decision

    async def drain(self, timeout_s: float | None = None):
        """Stop accepting new work and wait for draining."""
        self._draining = True
        log.info("Backpressure valve: entering DRAIN mode")
        t0 = time.time()
        timeout = timeout_s or self._drain_timeout
        while sum(self._queue_depths.values()) > 0:
            if (time.time() - t0) > timeout:
                log.warning("Drain timeout exceeded")
                break
            await asyncio.sleep(0.5)
        self._draining = False
        log.info("Backpressure valve: drain complete")

    def force_level(self, level: PressureLevel):
        """Manually override pressure level (for testing/ops)."""
        old = self._current_level
        self._current_level = level
        self._notify_level_change(old, level)

    def current_signals(self) -> list[dict]:
        return [s.to_dict() for s in self._signals.values()]

    def stats(self) -> dict:
        total = sum(
            self._stats[k] for k in ("accepted", "throttled", "queued", "rejected")
        )
        composite, level = self._composite_pressure()
        return {
            **self._stats,
            "total": total,
            "current_level": self._current_level.value,
            "composite_pressure": round(composite, 3),
            "draining": self._draining,
            "active_signals": len(self._signals),
        }


def build_jarvis_backpressure_valve() -> BackpressureValve:
    valve = BackpressureValve(max_queue_depth=500)

    valve.register_signal(
        PressureSignal(
            "queue_depth",
            0.0,
            threshold_yellow=100,
            threshold_orange=300,
            threshold_red=500,
            weight=2.0,
        )
    )
    valve.register_signal(
        PressureSignal(
            "cpu_pct",
            0.0,
            threshold_yellow=70.0,
            threshold_orange=85.0,
            threshold_red=95.0,
            weight=1.5,
        )
    )
    valve.register_signal(
        PressureSignal(
            "ram_pct",
            0.0,
            threshold_yellow=75.0,
            threshold_orange=88.0,
            threshold_red=96.0,
            weight=1.0,
        )
    )
    valve.register_signal(
        PressureSignal(
            "gpu_vram_pct",
            0.0,
            threshold_yellow=80.0,
            threshold_orange=90.0,
            threshold_red=98.0,
            weight=1.5,
        )
    )

    def log_level_change(old: PressureLevel, new: PressureLevel):
        log.warning(f"[BackpressureValve] {old.value} → {new.value}")

    valve.on_level_change(log_level_change)
    return valve


async def main():
    import sys

    valve = build_jarvis_backpressure_valve()
    await valve.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Simulating pressure escalation...")
        stages = [
            ("idle", 0, 10, 40, 30),
            ("queue growing", 150, 40, 55, 50),
            ("high load", 350, 75, 80, 85),
            ("critical", 480, 92, 90, 97),
            ("recovering", 50, 30, 50, 40),
        ]
        for label, queue, cpu, ram, gpu in stages:
            valve.update_signal("queue_depth", queue)
            valve.update_signal("cpu_pct", cpu)
            valve.update_signal("ram_pct", ram)
            valve.update_signal("gpu_vram_pct", gpu)
            d = valve.check()
            icon = {"green": "🟢", "yellow": "🟡", "orange": "🟠", "red": "🔴"}.get(
                d.level.value, "?"
            )
            print(
                f"  {icon} [{label:<18}] level={d.level.value:<8} "
                f"action={d.action.value:<10} pressure={d.composite_pressure:.3f} "
                f"throttle={d.throttle_ms:.0f}ms"
            )

        print("\nSignals:")
        for s in valve.current_signals():
            print(
                f"  {s['source']:<16} value={s['value']:<6} level={s['level']:<8} norm={s['normalized']}"
            )

        print(f"\nStats: {json.dumps(valve.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(valve.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
