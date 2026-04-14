#!/usr/bin/env python3
"""
jarvis_inference_limiter — Multi-level rate limiting for LLM inference requests
Per-model, per-agent, and global limits with burst allowance and backoff signals
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.inference_limiter")

REDIS_PREFIX = "jarvis:ratelimit:"


class LimitScope(str, Enum):
    GLOBAL = "global"
    PER_MODEL = "per_model"
    PER_AGENT = "per_agent"
    PER_IP = "per_ip"


class LimitDecision(str, Enum):
    ALLOW = "allow"
    THROTTLE = "throttle"  # allow but signal slow-down
    REJECT = "reject"  # over limit
    QUEUE = "queue"  # hold and retry


@dataclass
class LimitRule:
    name: str
    scope: LimitScope
    max_rps: float  # requests per second
    burst_size: int = 10  # allowed burst above steady rate
    window_s: float = 1.0  # sliding window
    backoff_s: float = 1.0  # suggested backoff on throttle
    hard_reject_at: float = 2.0  # multiple of max_rps where we hard-reject


@dataclass
class LimitResult:
    decision: LimitDecision
    scope: LimitScope
    key: str
    current_rps: float
    limit_rps: float
    remaining_burst: int
    retry_after_s: float = 0.0
    rule_name: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision in (LimitDecision.ALLOW, LimitDecision.THROTTLE)

    def to_dict(self) -> dict:
        return {
            "decision": self.decision.value,
            "scope": self.scope.value,
            "key": self.key,
            "current_rps": round(self.current_rps, 2),
            "limit_rps": self.limit_rps,
            "remaining_burst": self.remaining_burst,
            "retry_after_s": round(self.retry_after_s, 2),
        }


class _SlidingWindow:
    def __init__(self, window_s: float, burst_size: int):
        self._window = window_s
        self._burst = burst_size
        self._timestamps: list[float] = []
        self._lock = asyncio.Lock()

    async def add_and_check(
        self, max_rps: float, hard_reject_at: float
    ) -> tuple[LimitDecision, float, int]:
        async with self._lock:
            now = time.time()
            cutoff = now - self._window
            self._timestamps = [t for t in self._timestamps if t >= cutoff]

            current_rps = len(self._timestamps) / self._window
            burst_remaining = max(0, self._burst - len(self._timestamps))

            if current_rps >= max_rps * hard_reject_at:
                return LimitDecision.REJECT, current_rps, burst_remaining

            self._timestamps.append(now)
            new_rps = len(self._timestamps) / self._window

            if new_rps > max_rps:
                return LimitDecision.THROTTLE, new_rps, max(0, burst_remaining - 1)

            return LimitDecision.ALLOW, new_rps, burst_remaining

    def current_rps(self, window_s: float | None = None) -> float:
        w = window_s or self._window
        cutoff = time.time() - w
        recent = [t for t in self._timestamps if t >= cutoff]
        return len(recent) / w


class InferenceLimiter:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._rules: dict[str, LimitRule] = {}
        self._windows: dict[str, _SlidingWindow] = {}  # scope_key → window
        self._alert_callbacks: list[Callable] = []
        self._stats: dict[str, int] = {
            "allowed": 0,
            "throttled": 0,
            "rejected": 0,
            "total": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def add_rule(self, rule: LimitRule):
        self._rules[rule.name] = rule

    def on_reject(self, callback: Callable):
        self._alert_callbacks.append(callback)

    def _window_key(self, scope: LimitScope, key: str) -> str:
        return f"{scope.value}:{key}"

    def _get_window(self, wkey: str, rule: LimitRule) -> _SlidingWindow:
        if wkey not in self._windows:
            self._windows[wkey] = _SlidingWindow(rule.window_s, rule.burst_size)
        return self._windows[wkey]

    async def check(
        self,
        model: str = "",
        agent: str = "",
        ip: str = "",
    ) -> LimitResult:
        self._stats["total"] += 1
        worst_decision = LimitDecision.ALLOW
        worst_result: LimitResult | None = None

        checks = []
        for rule in self._rules.values():
            if rule.scope == LimitScope.GLOBAL:
                checks.append((rule, "global"))
            elif rule.scope == LimitScope.PER_MODEL and model:
                checks.append((rule, model))
            elif rule.scope == LimitScope.PER_AGENT and agent:
                checks.append((rule, agent))
            elif rule.scope == LimitScope.PER_IP and ip:
                checks.append((rule, ip))

        _priority = {
            LimitDecision.REJECT: 3,
            LimitDecision.THROTTLE: 2,
            LimitDecision.QUEUE: 1,
            LimitDecision.ALLOW: 0,
        }

        for rule, key in checks:
            wkey = self._window_key(rule.scope, key)
            window = self._get_window(wkey, rule)
            decision, rps, burst = await window.add_and_check(
                rule.max_rps, rule.hard_reject_at
            )

            retry_after = (
                rule.backoff_s
                if decision == LimitDecision.THROTTLE
                else (
                    1.0 / max(rule.max_rps, 0.1)
                    if decision == LimitDecision.REJECT
                    else 0.0
                )
            )

            result = LimitResult(
                decision=decision,
                scope=rule.scope,
                key=key,
                current_rps=rps,
                limit_rps=rule.max_rps,
                remaining_burst=burst,
                retry_after_s=retry_after,
                rule_name=rule.name,
            )

            if _priority.get(decision, 0) > _priority.get(worst_decision, 0):
                worst_decision = decision
                worst_result = result

        if worst_result is None:
            worst_result = LimitResult(
                LimitDecision.ALLOW, LimitScope.GLOBAL, "default", 0, 0, 0
            )

        if worst_decision == LimitDecision.ALLOW:
            self._stats["allowed"] += 1
        elif worst_decision == LimitDecision.THROTTLE:
            self._stats["throttled"] += 1
        elif worst_decision == LimitDecision.REJECT:
            self._stats["rejected"] += 1
            for cb in self._alert_callbacks:
                try:
                    cb(worst_result)
                except Exception:
                    pass
            if self.redis:
                asyncio.create_task(
                    self.redis.incr(f"{REDIS_PREFIX}rejected:{model or agent or ip}")
                )

        return worst_result

    def current_rps(self, scope: LimitScope, key: str) -> float:
        wkey = self._window_key(scope, key)
        window = self._windows.get(wkey)
        return window.current_rps() if window else 0.0

    def all_rps(self) -> dict[str, float]:
        return {k: w.current_rps() for k, w in self._windows.items()}

    def stats(self) -> dict:
        total = self._stats["total"]
        reject_rate = self._stats["rejected"] / max(total, 1)
        return {
            **self._stats,
            "reject_rate": round(reject_rate, 4),
            "rules": len(self._rules),
            "active_windows": len(self._windows),
        }


def build_jarvis_limiter() -> InferenceLimiter:
    limiter = InferenceLimiter()
    limiter.add_rule(
        LimitRule("global", LimitScope.GLOBAL, max_rps=50.0, burst_size=20)
    )
    limiter.add_rule(
        LimitRule("per_model_fast", LimitScope.PER_MODEL, max_rps=20.0, burst_size=10)
    )
    limiter.add_rule(
        LimitRule("per_model_slow", LimitScope.PER_MODEL, max_rps=5.0, burst_size=3)
    )
    limiter.add_rule(
        LimitRule("per_agent", LimitScope.PER_AGENT, max_rps=10.0, burst_size=5)
    )

    def log_reject(result: LimitResult):
        log.warning(
            f"Rate limit REJECT: {result.key} rps={result.current_rps:.1f}/{result.limit_rps}"
        )

    limiter.on_reject(log_reject)
    return limiter


async def main():
    import sys

    limiter = build_jarvis_limiter()
    await limiter.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Simulating 30 rapid requests to qwen3.5-9b from agent_x...")
        counts = {"allow": 0, "throttle": 0, "reject": 0}
        for i in range(30):
            r = await limiter.check(model="qwen3.5-9b", agent="agent_x")
            counts[r.decision.value] = counts.get(r.decision.value, 0) + 1
            if i < 5 or r.decision != LimitDecision.ALLOW:
                icon = {"allow": "✅", "throttle": "⚠️", "reject": "❌"}.get(
                    r.decision.value, "?"
                )
                print(
                    f"  [{i + 1:2d}] {icon} {r.decision.value:<10} rps={r.current_rps:.1f}/{r.limit_rps} burst={r.remaining_burst}"
                )

        print(f"\nSummary: {counts}")
        print("\nCurrent RPS by window:")
        for k, rps in sorted(limiter.all_rps().items()):
            print(f"  {k:<30} {rps:.2f} rps")

        print(f"\nStats: {json.dumps(limiter.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(limiter.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
