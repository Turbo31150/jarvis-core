#!/usr/bin/env python3
"""
jarvis_traffic_shaper — Traffic shaping: rate limiting, throttling, and admission control
Per-key token buckets, sliding windows, burst allowance, and queue draining
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.traffic_shaper")

REDIS_PREFIX = "jarvis:shaper:"


class ShapeAction(str, Enum):
    ALLOW = "allow"
    THROTTLE = "throttle"  # delay by throttle_ms
    QUEUE = "queue"  # queue and process later
    REJECT = "reject"  # drop the request


class ShapePolicy(str, Enum):
    TOKEN_BUCKET = "token_bucket"
    SLIDING_WINDOW = "sliding_window"
    FIXED_WINDOW = "fixed_window"
    CONCURRENCY = "concurrency"  # limit in-flight requests


@dataclass
class ShapeRule:
    rule_id: str
    key_pattern: str  # exact key or prefix (e.g. "agent:trading*")
    policy: ShapePolicy
    # Token bucket params
    rate: float = 10.0  # tokens/second or requests/window
    burst: float = 20.0  # max burst tokens
    window_s: float = 1.0  # for sliding/fixed window
    max_concurrent: int = 0  # for CONCURRENCY policy
    # Action params
    action_on_exceed: ShapeAction = ShapeAction.THROTTLE
    throttle_ms: float = 500.0
    priority: int = 5


@dataclass
class TokenBucketState:
    tokens: float
    capacity: float
    rate: float  # tokens/second
    last_refill: float = field(default_factory=time.time)

    def refill(self):
        now = time.time()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.last_refill = now

    def consume(self, amount: float = 1.0) -> bool:
        self.refill()
        if self.tokens >= amount:
            self.tokens -= amount
            return True
        return False

    def tokens_until_available(self, amount: float = 1.0) -> float:
        self.refill()
        deficit = amount - self.tokens
        if deficit <= 0:
            return 0.0
        return deficit / self.rate


@dataclass
class SlidingWindowState:
    window_s: float
    max_requests: int
    timestamps: list[float] = field(default_factory=list)

    def record(self):
        self.timestamps.append(time.time())

    def _purge(self):
        cutoff = time.time() - self.window_s
        self.timestamps = [t for t in self.timestamps if t >= cutoff]

    def count(self) -> int:
        self._purge()
        return len(self.timestamps)

    def can_admit(self) -> bool:
        return self.count() < self.max_requests

    def wait_s(self) -> float:
        self._purge()
        if len(self.timestamps) < self.max_requests:
            return 0.0
        oldest = self.timestamps[0]
        return max(0.0, oldest + self.window_s - time.time())


@dataclass
class ShapeDecision:
    key: str
    action: ShapeAction
    rule_id: str = ""
    throttle_ms: float = 0.0
    wait_s: float = 0.0
    tokens_remaining: float = 0.0
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "action": self.action.value,
            "rule_id": self.rule_id,
            "throttle_ms": round(self.throttle_ms, 1),
            "wait_s": round(self.wait_s, 3),
            "tokens_remaining": round(self.tokens_remaining, 2),
            "reason": self.reason,
        }


class TrafficShaper:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._rules: list[ShapeRule] = []
        self._buckets: dict[str, TokenBucketState] = {}
        self._windows: dict[str, SlidingWindowState] = {}
        self._concurrent: dict[str, int] = {}  # key → active count
        self._stats: dict[str, int] = {
            "allowed": 0,
            "throttled": 0,
            "queued": 0,
            "rejected": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def add_rule(self, rule: ShapeRule):
        self._rules.append(rule)
        self._rules.sort(key=lambda r: r.priority)

    def _match_rule(self, key: str) -> ShapeRule | None:
        for rule in self._rules:
            pattern = rule.key_pattern
            if pattern.endswith("*"):
                if key.startswith(pattern[:-1]):
                    return rule
            elif key == pattern or pattern == "*":
                return rule
        return None

    def _check_token_bucket(self, key: str, rule: ShapeRule) -> ShapeDecision:
        if key not in self._buckets:
            self._buckets[key] = TokenBucketState(
                tokens=rule.burst,
                capacity=rule.burst,
                rate=rule.rate,
            )
        bucket = self._buckets[key]
        if bucket.consume():
            return ShapeDecision(
                key=key,
                action=ShapeAction.ALLOW,
                rule_id=rule.rule_id,
                tokens_remaining=bucket.tokens,
            )
        wait = bucket.tokens_until_available()
        return ShapeDecision(
            key=key,
            action=rule.action_on_exceed,
            rule_id=rule.rule_id,
            throttle_ms=rule.throttle_ms,
            wait_s=wait,
            tokens_remaining=bucket.tokens,
            reason=f"token bucket empty, refill in {wait:.2f}s",
        )

    def _check_sliding_window(self, key: str, rule: ShapeRule) -> ShapeDecision:
        if key not in self._windows:
            self._windows[key] = SlidingWindowState(
                window_s=rule.window_s,
                max_requests=int(rule.rate),
            )
        window = self._windows[key]
        if window.can_admit():
            window.record()
            return ShapeDecision(
                key=key, action=ShapeAction.ALLOW, rule_id=rule.rule_id
            )
        wait = window.wait_s()
        return ShapeDecision(
            key=key,
            action=rule.action_on_exceed,
            rule_id=rule.rule_id,
            throttle_ms=rule.throttle_ms,
            wait_s=wait,
            reason=f"rate limit: {window.count()}/{int(rule.rate)} in {rule.window_s}s",
        )

    def _check_concurrency(self, key: str, rule: ShapeRule) -> ShapeDecision:
        active = self._concurrent.get(key, 0)
        if active < rule.max_concurrent:
            self._concurrent[key] = active + 1
            return ShapeDecision(
                key=key, action=ShapeAction.ALLOW, rule_id=rule.rule_id
            )
        return ShapeDecision(
            key=key,
            action=rule.action_on_exceed,
            rule_id=rule.rule_id,
            throttle_ms=rule.throttle_ms,
            reason=f"concurrency limit: {active}/{rule.max_concurrent}",
        )

    def check(self, key: str) -> ShapeDecision:
        rule = self._match_rule(key)
        if not rule:
            return ShapeDecision(key=key, action=ShapeAction.ALLOW)

        if rule.policy == ShapePolicy.TOKEN_BUCKET:
            decision = self._check_token_bucket(key, rule)
        elif rule.policy == ShapePolicy.SLIDING_WINDOW:
            decision = self._check_sliding_window(key, rule)
        elif rule.policy == ShapePolicy.CONCURRENCY:
            decision = self._check_concurrency(key, rule)
        else:
            decision = self._check_sliding_window(key, rule)

        action = decision.action
        if action == ShapeAction.ALLOW:
            self._stats["allowed"] += 1
        elif action == ShapeAction.THROTTLE:
            self._stats["throttled"] += 1
        elif action == ShapeAction.QUEUE:
            self._stats["queued"] += 1
        elif action == ShapeAction.REJECT:
            self._stats["rejected"] += 1

        return decision

    async def check_and_wait(self, key: str) -> ShapeDecision:
        """Like check() but automatically waits if throttled."""
        decision = self.check(key)
        if decision.action == ShapeAction.THROTTLE and decision.throttle_ms > 0:
            await asyncio.sleep(decision.throttle_ms / 1000)
            decision.action = ShapeAction.ALLOW
        elif decision.action == ShapeAction.QUEUE and decision.wait_s > 0:
            await asyncio.sleep(decision.wait_s)
            decision.action = ShapeAction.ALLOW
        return decision

    def release(self, key: str):
        """Call after completing a request for CONCURRENCY policy."""
        if key in self._concurrent:
            self._concurrent[key] = max(0, self._concurrent[key] - 1)

    def stats(self) -> dict:
        total = sum(self._stats.values())
        return {
            **self._stats,
            "total": total,
            "reject_rate": round(self._stats["rejected"] / max(total, 1), 4),
            "rules": len(self._rules),
            "tracked_keys": len(self._buckets)
            + len(self._windows)
            + len(self._concurrent),
        }


def build_jarvis_traffic_shaper() -> TrafficShaper:
    shaper = TrafficShaper()

    shaper.add_rule(
        ShapeRule(
            rule_id="trading-bucket",
            key_pattern="trading*",
            policy=ShapePolicy.TOKEN_BUCKET,
            rate=5.0,
            burst=10.0,
            action_on_exceed=ShapeAction.THROTTLE,
            throttle_ms=200.0,
            priority=1,
        )
    )
    shaper.add_rule(
        ShapeRule(
            rule_id="inference-window",
            key_pattern="inference*",
            policy=ShapePolicy.SLIDING_WINDOW,
            rate=30.0,
            window_s=1.0,
            action_on_exceed=ShapeAction.QUEUE,
            priority=2,
        )
    )
    shaper.add_rule(
        ShapeRule(
            rule_id="admin-concurrency",
            key_pattern="admin*",
            policy=ShapePolicy.CONCURRENCY,
            max_concurrent=2,
            action_on_exceed=ShapeAction.REJECT,
            priority=1,
        )
    )
    shaper.add_rule(
        ShapeRule(
            rule_id="global-default",
            key_pattern="*",
            policy=ShapePolicy.TOKEN_BUCKET,
            rate=100.0,
            burst=200.0,
            action_on_exceed=ShapeAction.THROTTLE,
            throttle_ms=100.0,
            priority=10,
        )
    )

    return shaper


async def main():
    import sys

    shaper = build_jarvis_traffic_shaper()
    await shaper.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Traffic shaping demo...")
        keys = ["trading-signal", "inference-gw", "admin-cli", "monitor-health"]

        for i in range(15):
            key = keys[i % len(keys)]
            decision = shaper.check(key)
            icon = {"allow": "✅", "throttle": "🐢", "queue": "⏳", "reject": "❌"}.get(
                decision.action.value, "?"
            )
            print(
                f"  {icon} {key:<20} → {decision.action.value:<10} {decision.reason or ''}"
            )
            if decision.action == ShapeAction.CONCURRENCY:
                shaper.release(key)

        print(f"\nStats: {json.dumps(shaper.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(shaper.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
