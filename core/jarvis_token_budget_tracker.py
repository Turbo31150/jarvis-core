#!/usr/bin/env python3
"""
jarvis_token_budget_tracker — Per-user/model/project token budget enforcement
Tracks consumption, enforces limits, alerts on approaching thresholds
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.token_budget_tracker")

REDIS_PREFIX = "jarvis:budget:"
BUDGET_FILE = Path("/home/turbo/IA/Core/jarvis/data/token_budgets.jsonl")


class BudgetPeriod(str, Enum):
    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    TOTAL = "total"


class BudgetStatus(str, Enum):
    OK = "ok"
    WARNING = "warning"  # >= warn_threshold_pct
    CRITICAL = "critical"  # >= critical_threshold_pct
    EXCEEDED = "exceeded"


@dataclass
class Budget:
    budget_id: str
    entity: str  # user_id, model_name, project_id
    entity_type: str  # user | model | project
    max_tokens: int
    period: BudgetPeriod
    warn_threshold_pct: float = 80.0
    critical_threshold_pct: float = 95.0
    hard_limit: bool = True  # if True, reject requests when exceeded
    created_at: float = field(default_factory=time.time)

    def period_start(self) -> float:
        now = time.time()
        import datetime

        dt = datetime.datetime.utcfromtimestamp(now)
        if self.period == BudgetPeriod.HOURLY:
            return dt.replace(minute=0, second=0, microsecond=0).timestamp()
        elif self.period == BudgetPeriod.DAILY:
            return dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        elif self.period == BudgetPeriod.WEEKLY:
            start = dt - datetime.timedelta(days=dt.weekday())
            return start.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        elif self.period == BudgetPeriod.MONTHLY:
            return dt.replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            ).timestamp()
        else:  # TOTAL
            return 0.0

    def to_dict(self) -> dict:
        return {
            "budget_id": self.budget_id,
            "entity": self.entity,
            "entity_type": self.entity_type,
            "max_tokens": self.max_tokens,
            "period": self.period.value,
            "hard_limit": self.hard_limit,
        }


@dataclass
class TokenUsage:
    budget_id: str
    tokens: int
    model: str
    ts: float = field(default_factory=time.time)
    request_id: str = ""

    def to_dict(self) -> dict:
        return {
            "budget_id": self.budget_id,
            "tokens": self.tokens,
            "model": self.model,
            "ts": self.ts,
            "request_id": self.request_id,
        }


@dataclass
class BudgetState:
    budget: Budget
    used: int
    status: BudgetStatus
    pct_used: float
    remaining: int
    period_start: float
    period_end: float

    def to_dict(self) -> dict:
        return {
            "budget_id": self.budget.budget_id,
            "entity": self.budget.entity,
            "max_tokens": self.budget.max_tokens,
            "used": self.used,
            "remaining": self.remaining,
            "pct_used": round(self.pct_used, 2),
            "status": self.status.value,
            "period": self.budget.period.value,
            "period_start": self.period_start,
        }


class TokenBudgetTracker:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._budgets: dict[str, Budget] = {}
        self._usage: list[TokenUsage] = []
        self._alert_callbacks: list[Any] = []
        self._stats: dict[str, int] = {
            "requests": 0,
            "rejected": 0,
            "alerts_fired": 0,
            "total_tokens": 0,
        }
        BUDGET_FILE.parent.mkdir(parents=True, exist_ok=True)

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def define_budget(self, budget: Budget):
        self._budgets[budget.budget_id] = budget
        log.info(
            f"Budget: {budget.budget_id} max={budget.max_tokens} period={budget.period.value}"
        )

    def on_alert(self, callback):
        self._alert_callbacks.append(callback)

    def _used_in_period(self, budget_id: str, since: float) -> int:
        return sum(
            u.tokens for u in self._usage if u.budget_id == budget_id and u.ts >= since
        )

    def get_state(self, budget_id: str) -> BudgetState | None:
        budget = self._budgets.get(budget_id)
        if not budget:
            return None

        period_start = budget.period_start()
        import datetime

        dt_start = datetime.datetime.utcfromtimestamp(period_start)
        if budget.period == BudgetPeriod.HOURLY:
            period_end = period_start + 3600
        elif budget.period == BudgetPeriod.DAILY:
            period_end = period_start + 86400
        elif budget.period == BudgetPeriod.WEEKLY:
            period_end = period_start + 7 * 86400
        elif budget.period == BudgetPeriod.MONTHLY:
            import calendar

            _, days = calendar.monthrange(dt_start.year, dt_start.month)
            period_end = period_start + days * 86400
        else:
            period_end = float("inf")

        used = self._used_in_period(budget_id, period_start)
        pct = used / max(budget.max_tokens, 1) * 100

        if pct >= 100:
            status = BudgetStatus.EXCEEDED
        elif pct >= budget.critical_threshold_pct:
            status = BudgetStatus.CRITICAL
        elif pct >= budget.warn_threshold_pct:
            status = BudgetStatus.WARNING
        else:
            status = BudgetStatus.OK

        return BudgetState(
            budget=budget,
            used=used,
            status=status,
            pct_used=pct,
            remaining=max(0, budget.max_tokens - used),
            period_start=period_start,
            period_end=period_end,
        )

    def check(
        self, budget_id: str, tokens_requested: int
    ) -> tuple[bool, BudgetState | None]:
        """Returns (allowed, state). allowed=False if hard limit exceeded."""
        self._stats["requests"] += 1
        state = self.get_state(budget_id)
        if not state:
            return True, None  # no budget defined = allow

        if state.budget.hard_limit and state.status == BudgetStatus.EXCEEDED:
            self._stats["rejected"] += 1
            return False, state

        if state.budget.hard_limit and tokens_requested > state.remaining:
            self._stats["rejected"] += 1
            return False, state

        return True, state

    def record(
        self,
        budget_id: str,
        tokens: int,
        model: str = "",
        request_id: str = "",
    ) -> BudgetState | None:
        usage = TokenUsage(
            budget_id=budget_id,
            tokens=tokens,
            model=model,
            request_id=request_id,
        )
        self._usage.append(usage)
        self._stats["total_tokens"] += tokens

        # Keep memory manageable
        if len(self._usage) > 100_000:
            cutoff = time.time() - 31 * 86400
            self._usage = [u for u in self._usage if u.ts >= cutoff]

        # Persist
        with open(BUDGET_FILE, "a") as f:
            f.write(json.dumps(usage.to_dict()) + "\n")

        if self.redis:
            asyncio.create_task(
                self.redis.hincrby(f"{REDIS_PREFIX}{budget_id}", "tokens", tokens)
            )

        state = self.get_state(budget_id)
        if state and state.status in (
            BudgetStatus.WARNING,
            BudgetStatus.CRITICAL,
            BudgetStatus.EXCEEDED,
        ):
            self._fire_alert(budget_id, state)

        return state

    def _fire_alert(self, budget_id: str, state: BudgetState):
        self._stats["alerts_fired"] += 1
        for cb in self._alert_callbacks:
            try:
                cb(budget_id, state)
            except Exception:
                pass

    def all_states(self) -> list[dict]:
        result = []
        for bid in self._budgets:
            s = self.get_state(bid)
            if s:
                result.append(s.to_dict())
        return result

    def stats(self) -> dict:
        return {**self._stats, "budgets": len(self._budgets)}


def build_jarvis_budget_tracker() -> TokenBudgetTracker:
    tracker = TokenBudgetTracker()
    tracker.define_budget(
        Budget("global_daily", "system", "project", 2_000_000, BudgetPeriod.DAILY)
    )
    tracker.define_budget(
        Budget("m1_hourly", "m1", "model", 500_000, BudgetPeriod.HOURLY)
    )
    tracker.define_budget(
        Budget(
            "turbo_daily",
            "turbo",
            "user",
            300_000,
            BudgetPeriod.DAILY,
            hard_limit=False,
        )
    )
    return tracker


async def main():
    import sys
    import random

    tracker = build_jarvis_budget_tracker()
    await tracker.connect_redis()

    def alert_cb(bid, state):
        print(f"  ⚠️  ALERT {bid}: {state.status.value} ({state.pct_used:.1f}%)")

    tracker.on_alert(alert_cb)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        random.seed(42)
        print("Simulating token usage...")
        for i in range(100):
            tokens = random.randint(500, 5000)
            allowed, state = tracker.check("turbo_daily", tokens)
            if allowed:
                state = tracker.record("turbo_daily", tokens, model="qwen3.5")
                tracker.record("global_daily", tokens, model="qwen3.5")

        print("\nBudget states:")
        for s in tracker.all_states():
            icon = {"ok": "✅", "warning": "⚠️", "critical": "🔴", "exceeded": "🚫"}.get(
                s["status"], "?"
            )
            print(
                f"  {icon} {s['budget_id']:<20} {s['used']:>8}/{s['max_tokens']:<10} ({s['pct_used']:.1f}%)"
            )

        print(f"\nStats: {json.dumps(tracker.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(tracker.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
