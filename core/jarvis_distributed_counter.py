#!/usr/bin/env python3
"""
jarvis_distributed_counter — Distributed counters with Redis INCR, CRDTs, and windowed rates
Token counting, request rates, budget tracking across cluster nodes
"""

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.distributed_counter")

REDIS_PREFIX = "jarvis:cnt:"


class CounterKind(str, Enum):
    SIMPLE = "simple"  # plain increment/decrement
    RATE = "rate"  # events/second over sliding window
    BUDGET = "budget"  # budget with cap enforcement
    GCOUNTER = "gcounter"  # G-Counter CRDT (merge-only increment)


@dataclass
class CounterConfig:
    name: str
    kind: CounterKind = CounterKind.SIMPLE
    window_s: float = 60.0  # for RATE counters
    cap: float = 0.0  # for BUDGET (0 = no cap)
    initial: float = 0.0
    persist: bool = True


@dataclass
class CounterSnapshot:
    name: str
    kind: CounterKind
    value: float
    rate: float = 0.0  # events/s for RATE counters
    cap: float = 0.0
    pct_used: float = 0.0  # for BUDGET
    ts: float = field(default_factory=time.time)
    node_values: dict[str, float] = field(default_factory=dict)  # for GCOUNTER

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind.value,
            "value": round(self.value, 6),
            "rate": round(self.rate, 4),
            "cap": self.cap,
            "pct_used": round(self.pct_used, 4),
            "ts": self.ts,
            "node_values": self.node_values,
        }


class DistributedCounter:
    def __init__(self, node_id: str = "local"):
        self.redis: aioredis.Redis | None = None
        self._node_id = node_id
        self._configs: dict[str, CounterConfig] = {}
        self._local: dict[str, float] = {}  # local values
        self._windows: dict[str, deque] = {}  # RATE: timestamps
        self._gcounter: dict[str, dict[str, float]] = {}  # GCOUNTER: node→value
        self._stats: dict[str, int] = {
            "increments": 0,
            "decrements": 0,
            "resets": 0,
            "redis_syncs": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def define(self, config: CounterConfig):
        self._configs[config.name] = config
        self._local[config.name] = config.initial
        if config.kind == CounterKind.RATE:
            self._windows[config.name] = deque()
        elif config.kind == CounterKind.GCOUNTER:
            self._gcounter[config.name] = {self._node_id: 0.0}

    def _get_or_define(self, name: str) -> CounterConfig:
        if name not in self._configs:
            cfg = CounterConfig(name=name)
            self.define(cfg)
        return self._configs[name]

    async def increment(self, name: str, amount: float = 1.0) -> float:
        cfg = self._get_or_define(name)
        self._stats["increments"] += 1

        if cfg.kind == CounterKind.RATE:
            now = time.time()
            win = self._windows[name]
            win.append(now)
            # Purge old
            cutoff = now - cfg.window_s
            while win and win[0] < cutoff:
                win.popleft()
            self._local[name] = float(len(win))

        elif cfg.kind == CounterKind.GCOUNTER:
            self._gcounter[name][self._node_id] = (
                self._gcounter[name].get(self._node_id, 0.0) + amount
            )
            self._local[name] = sum(self._gcounter[name].values())

        elif cfg.kind == CounterKind.BUDGET:
            new_val = self._local[name] + amount
            if cfg.cap > 0 and new_val > cfg.cap:
                return self._local[name]  # cap exceeded — reject
            self._local[name] = new_val

        else:
            self._local[name] += amount

        if self.redis and cfg.persist:
            asyncio.create_task(self._redis_incr(name, amount, cfg))

        return self._local[name]

    async def decrement(self, name: str, amount: float = 1.0) -> float:
        cfg = self._get_or_define(name)
        self._stats["decrements"] += 1

        if cfg.kind in (CounterKind.RATE, CounterKind.GCOUNTER):
            return self._local.get(name, 0.0)  # these don't support decrement

        self._local[name] = self._local.get(name, 0.0) - amount
        if self.redis and cfg.persist:
            asyncio.create_task(self._redis_incr(name, -amount, cfg))
        return self._local[name]

    async def reset(self, name: str) -> bool:
        cfg = self._configs.get(name)
        if not cfg:
            return False
        self._local[name] = cfg.initial
        if cfg.kind == CounterKind.RATE and name in self._windows:
            self._windows[name].clear()
        elif cfg.kind == CounterKind.GCOUNTER:
            self._gcounter[name] = {self._node_id: 0.0}
        self._stats["resets"] += 1
        if self.redis and cfg.persist:
            asyncio.create_task(self._redis_reset(name, cfg.initial))
        return True

    async def get(self, name: str, sync_redis: bool = False) -> float:
        cfg = self._get_or_define(name)

        if cfg.kind == CounterKind.RATE:
            win = self._windows.get(name, deque())
            now = time.time()
            cutoff = now - cfg.window_s
            while win and win[0] < cutoff:
                win.popleft()
            self._local[name] = float(len(win))

        if sync_redis and self.redis and cfg.persist:
            val = await self._redis_get(name)
            if val is not None:
                self._local[name] = val

        return self._local.get(name, 0.0)

    def get_local(self, name: str) -> float:
        return self._local.get(name, 0.0)

    def rate(self, name: str) -> float:
        cfg = self._configs.get(name)
        if not cfg or cfg.kind != CounterKind.RATE:
            return 0.0
        win = self._windows.get(name, deque())
        now = time.time()
        cutoff = now - cfg.window_s
        active = sum(1 for ts in win if ts >= cutoff)
        return active / max(cfg.window_s, 1.0)

    def budget_remaining(self, name: str) -> float:
        cfg = self._configs.get(name)
        if not cfg or cfg.kind != CounterKind.BUDGET or cfg.cap <= 0:
            return float("inf")
        return max(0.0, cfg.cap - self._local.get(name, 0.0))

    def can_spend(self, name: str, amount: float) -> bool:
        cfg = self._configs.get(name)
        if not cfg or cfg.kind != CounterKind.BUDGET:
            return True
        if cfg.cap <= 0:
            return True
        return (self._local.get(name, 0.0) + amount) <= cfg.cap

    # GCOUNTER merge from another node
    def merge(self, name: str, node_id: str, value: float):
        if name not in self._gcounter:
            self._gcounter[name] = {}
        # G-Counter merge: take max per node
        self._gcounter[name][node_id] = max(
            self._gcounter[name].get(node_id, 0.0), value
        )
        self._local[name] = sum(self._gcounter[name].values())

    async def _redis_incr(self, name: str, amount: float, cfg: CounterConfig):
        if not self.redis:
            return
        try:
            key = f"{REDIS_PREFIX}{name}"
            if amount == int(amount):
                await self.redis.incrbyfloat(key, amount)
            else:
                await self.redis.incrbyfloat(key, amount)
            if cfg.window_s > 0:
                await self.redis.expire(key, int(cfg.window_s * 10))
            self._stats["redis_syncs"] += 1
        except Exception:
            pass

    async def _redis_reset(self, name: str, value: float):
        if not self.redis:
            return
        try:
            await self.redis.set(f"{REDIS_PREFIX}{name}", value)
        except Exception:
            pass

    async def _redis_get(self, name: str) -> float | None:
        if not self.redis:
            return None
        try:
            val = await self.redis.get(f"{REDIS_PREFIX}{name}")
            return float(val) if val is not None else None
        except Exception:
            return None

    async def snapshot_all(self) -> list[CounterSnapshot]:
        snapshots = []
        for name, cfg in self._configs.items():
            val = await self.get(name)
            rate = self.rate(name) if cfg.kind == CounterKind.RATE else 0.0
            pct = (val / cfg.cap) if cfg.cap > 0 else 0.0
            snap = CounterSnapshot(
                name=name,
                kind=cfg.kind,
                value=val,
                rate=rate,
                cap=cfg.cap,
                pct_used=pct,
                node_values=dict(self._gcounter.get(name, {})),
            )
            snapshots.append(snap)
        return snapshots

    def stats(self) -> dict:
        return {
            **self._stats,
            "counters": len(self._configs),
        }


def build_jarvis_distributed_counter(node_id: str = "m1") -> DistributedCounter:
    dc = DistributedCounter(node_id=node_id)

    dc.define(CounterConfig("tokens.input", CounterKind.BUDGET, cap=1_000_000))
    dc.define(CounterConfig("tokens.output", CounterKind.BUDGET, cap=500_000))
    dc.define(CounterConfig("requests.rate", CounterKind.RATE, window_s=60.0))
    dc.define(CounterConfig("errors.total", CounterKind.SIMPLE))
    dc.define(CounterConfig("inference.count", CounterKind.GCOUNTER))

    return dc


async def main():
    import sys

    dc = build_jarvis_distributed_counter()
    await dc.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Distributed counter demo...")

        # Budget
        for _ in range(100):
            await dc.increment("tokens.input", 1000)
        await dc.increment("tokens.output", 50000)

        # Rate
        for _ in range(10):
            await dc.increment("requests.rate")

        # GCounter
        await dc.increment("inference.count", 5)
        dc.merge("inference.count", "m2", 12.0)
        dc.merge("inference.count", "ol1", 3.0)

        # Budget overflow
        await dc.increment("tokens.input", 999_000_000)  # should be capped

        snaps = await dc.snapshot_all()
        print(f"\n  {'Counter':<25} {'Value':>12} {'Rate':>8} {'Cap':>12} {'%':>6}")
        for s in snaps:
            cap_str = f"{s.cap:.0f}" if s.cap else "∞"
            pct_str = f"{s.pct_used * 100:.1f}%" if s.cap else ""
            print(
                f"  {s.name:<25} {s.value:>12.1f} {s.rate:>8.3f} {cap_str:>12} {pct_str:>6}"
            )

        print(
            f"\n  can_spend tokens.input 100k → {dc.can_spend('tokens.input', 100_000)}"
        )
        print(
            f"  budget_remaining tokens.input → {dc.budget_remaining('tokens.input'):.0f}"
        )

        print(f"\nStats: {json.dumps(dc.stats(), indent=2)}")

    elif cmd == "snapshot":
        snaps = await dc.snapshot_all()
        print(json.dumps([s.to_dict() for s in snaps], indent=2))

    elif cmd == "stats":
        print(json.dumps(dc.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
