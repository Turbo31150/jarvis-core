#!/usr/bin/env python3
"""
jarvis_budget_tracker — Token and cost budget tracking for LLM usage
Per-agent, per-model, and global budgets with alerts and enforcement
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

log = logging.getLogger("jarvis.budget_tracker")

LEDGER_FILE = Path("/home/turbo/IA/Core/jarvis/data/token_ledger.jsonl")
REDIS_PREFIX = "jarvis:budget:"


class BudgetScope(str, Enum):
    GLOBAL = "global"
    PER_AGENT = "per_agent"
    PER_MODEL = "per_model"
    PER_SESSION = "per_session"


class BudgetStatus(str, Enum):
    OK = "ok"
    WARNING = "warning"  # > 80% consumed
    CRITICAL = "critical"  # > 95% consumed
    EXCEEDED = "exceeded"


# Cost per 1M tokens (USD) for common models
_MODEL_COSTS: dict[str, tuple[float, float]] = {
    # model: (input_cost_per_1M, output_cost_per_1M)
    "gpt-4o": (5.0, 15.0),
    "gpt-4o-mini": (0.15, 0.60),
    "claude-opus-4-6": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (0.25, 1.25),
    # Local models: zero cost
    "qwen3.5-9b": (0.0, 0.0),
    "qwen3.5-27b": (0.0, 0.0),
    "deepseek-r1-0528": (0.0, 0.0),
    "gemma3:4b": (0.0, 0.0),
}


def _model_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    # Find best prefix match
    matched = next(
        (costs for key, costs in _MODEL_COSTS.items() if key in model.lower()),
        (0.0, 0.0),
    )
    return (input_tokens * matched[0] + output_tokens * matched[1]) / 1_000_000


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    requests: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, input_t: int, output_t: int, cost: float):
        self.input_tokens += input_t
        self.output_tokens += output_t
        self.cost_usd += cost
        self.requests += 1

    def to_dict(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "requests": self.requests,
        }


@dataclass
class Budget:
    scope: BudgetScope
    key: str  # agent_id, model_id, session_id, or "global"
    max_tokens: int  # 0 = unlimited
    max_cost_usd: float  # 0 = unlimited
    window_s: float = 0.0  # 0 = no rolling window
    usage: TokenUsage = field(default_factory=TokenUsage)
    window_start: float = field(default_factory=time.time)
    alerts_sent: list[str] = field(default_factory=list)

    @property
    def token_pct(self) -> float:
        if self.max_tokens <= 0:
            return 0.0
        return self.usage.total_tokens / self.max_tokens * 100

    @property
    def cost_pct(self) -> float:
        if self.max_cost_usd <= 0:
            return 0.0
        return self.usage.cost_usd / self.max_cost_usd * 100

    @property
    def status(self) -> BudgetStatus:
        pct = max(self.token_pct, self.cost_pct)
        if pct >= 100:
            return BudgetStatus.EXCEEDED
        elif pct >= 95:
            return BudgetStatus.CRITICAL
        elif pct >= 80:
            return BudgetStatus.WARNING
        return BudgetStatus.OK

    def is_window_expired(self) -> bool:
        return self.window_s > 0 and (time.time() - self.window_start) > self.window_s

    def reset_window(self):
        self.usage = TokenUsage()
        self.window_start = time.time()
        self.alerts_sent = []

    def to_dict(self) -> dict:
        return {
            "scope": self.scope.value,
            "key": self.key,
            "max_tokens": self.max_tokens,
            "max_cost_usd": self.max_cost_usd,
            "usage": self.usage.to_dict(),
            "token_pct": round(self.token_pct, 1),
            "cost_pct": round(self.cost_pct, 1),
            "status": self.status.value,
        }


@dataclass
class LedgerEntry:
    ts: float
    agent_id: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    session_id: str = ""
    request_id: str = ""

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "agent_id": self.agent_id,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "session_id": self.session_id,
        }


class BudgetTracker:
    def __init__(self, enforce: bool = True, persist: bool = True):
        self.redis: aioredis.Redis | None = None
        self._budgets: dict[str, Budget] = {}  # f"{scope}:{key}" → Budget
        self._global_usage = TokenUsage()
        self._enforce = enforce
        self._persist = persist
        self._alert_callbacks: list = []
        self._stats: dict[str, Any] = {
            "records": 0,
            "enforced_blocks": 0,
            "alerts_fired": 0,
        }
        if persist:
            LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def set_budget(self, budget: Budget):
        key = f"{budget.scope.value}:{budget.key}"
        self._budgets[key] = budget

    def on_alert(self, callback):
        self._alert_callbacks.append(callback)

    def _budget_key(self, scope: BudgetScope, key: str) -> str:
        return f"{scope.value}:{key}"

    def _get_budget(self, scope: BudgetScope, key: str) -> Budget | None:
        return self._budgets.get(self._budget_key(scope, key))

    def _fire_alert(self, budget: Budget, threshold: str):
        if threshold in budget.alerts_sent:
            return
        budget.alerts_sent.append(threshold)
        self._stats["alerts_fired"] += 1
        msg = (
            f"Budget alert [{budget.scope.value}:{budget.key}] "
            f"{threshold} — tokens={budget.usage.total_tokens}/{budget.max_tokens} "
            f"cost=${budget.usage.cost_usd:.4f}/${budget.max_cost_usd:.4f}"
        )
        log.warning(msg)
        for cb in self._alert_callbacks:
            try:
                cb(budget, threshold)
            except Exception:
                pass

    def check_allowed(
        self, agent_id: str, model: str, estimated_tokens: int = 100
    ) -> bool:
        if not self._enforce:
            return True
        budgets_to_check = [
            self._get_budget(BudgetScope.GLOBAL, "global"),
            self._get_budget(BudgetScope.PER_AGENT, agent_id),
            self._get_budget(BudgetScope.PER_MODEL, model),
        ]
        for budget in budgets_to_check:
            if not budget:
                continue
            if budget.is_window_expired():
                budget.reset_window()
            if budget.max_tokens > 0:
                if budget.usage.total_tokens + estimated_tokens > budget.max_tokens:
                    self._stats["enforced_blocks"] += 1
                    log.warning(
                        f"Budget BLOCKED [{budget.scope.value}:{budget.key}]: "
                        f"would exceed token limit"
                    )
                    return False
        return True

    def record(
        self,
        agent_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        session_id: str = "",
        request_id: str = "",
    ):
        cost = _model_cost(model, input_tokens, output_tokens)

        # Update global usage
        self._global_usage.add(input_tokens, output_tokens, cost)
        self._stats["records"] += 1

        # Update relevant budgets
        for scope, key in [
            (BudgetScope.GLOBAL, "global"),
            (BudgetScope.PER_AGENT, agent_id),
            (BudgetScope.PER_MODEL, model),
            (BudgetScope.PER_SESSION, session_id),
        ]:
            if not key:
                continue
            budget = self._get_budget(scope, key)
            if not budget:
                continue
            if budget.is_window_expired():
                budget.reset_window()
            budget.usage.add(input_tokens, output_tokens, cost)

            # Fire alerts
            pct = max(budget.token_pct, budget.cost_pct)
            if pct >= 95 and "critical" not in budget.alerts_sent:
                self._fire_alert(budget, "critical")
            elif pct >= 80 and "warning" not in budget.alerts_sent:
                self._fire_alert(budget, "warning")

        # Ledger
        entry = LedgerEntry(
            ts=time.time(),
            agent_id=agent_id,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            session_id=session_id,
            request_id=request_id,
        )
        if self._persist:
            try:
                with open(LEDGER_FILE, "a") as f:
                    f.write(json.dumps(entry.to_dict()) + "\n")
            except Exception:
                pass

        if self.redis:
            asyncio.create_task(
                self._redis_record(agent_id, model, input_tokens, output_tokens, cost)
            )

    async def _redis_record(
        self, agent_id: str, model: str, in_t: int, out_t: int, cost: float
    ):
        if not self.redis:
            return
        try:
            pipe = self.redis.pipeline()
            pipe.hincrby(f"{REDIS_PREFIX}agent:{agent_id}", "input_tokens", in_t)
            pipe.hincrby(f"{REDIS_PREFIX}agent:{agent_id}", "output_tokens", out_t)
            pipe.hincrbyfloat(f"{REDIS_PREFIX}agent:{agent_id}", "cost_usd", cost)
            pipe.expire(f"{REDIS_PREFIX}agent:{agent_id}", 86400)
            await pipe.execute()
        except Exception:
            pass

    def budget_status(self) -> list[dict]:
        return [b.to_dict() for b in self._budgets.values()]

    def global_usage(self) -> dict:
        return {
            "global": self._global_usage.to_dict(),
            "by_budget": {k: b.usage.to_dict() for k, b in self._budgets.items()},
        }

    def stats(self) -> dict:
        return {
            **self._stats,
            "total_tokens": self._global_usage.total_tokens,
            "total_cost_usd": round(self._global_usage.cost_usd, 6),
            "budgets": len(self._budgets),
        }


def build_jarvis_budget_tracker() -> BudgetTracker:
    tracker = BudgetTracker(enforce=True, persist=True)

    tracker.set_budget(
        Budget(
            scope=BudgetScope.GLOBAL,
            key="global",
            max_tokens=10_000_000,
            max_cost_usd=5.0,
            window_s=86400.0,  # daily
        )
    )
    tracker.set_budget(
        Budget(
            scope=BudgetScope.PER_AGENT,
            key="trading-agent",
            max_tokens=500_000,
            max_cost_usd=1.0,
            window_s=3600.0,
        )
    )
    tracker.set_budget(
        Budget(
            scope=BudgetScope.PER_MODEL,
            key="claude-sonnet-4-6",
            max_tokens=100_000,
            max_cost_usd=2.0,
            window_s=3600.0,
        )
    )

    def alert_log(budget: Budget, threshold: str):
        log.warning(f"🚨 Budget {threshold}: {budget.scope.value}:{budget.key}")

    tracker.on_alert(alert_log)
    return tracker


async def main():
    import sys

    tracker = build_jarvis_budget_tracker()
    await tracker.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Recording token usage...")
        test_cases = [
            ("trading-agent", "qwen3.5-9b", 500, 200),
            ("trading-agent", "qwen3.5-9b", 1000, 400),
            ("inference-gw", "claude-sonnet-4-6", 2000, 800),
            ("inference-gw", "qwen3.5-27b", 800, 300),
            ("analysis-agent", "deepseek-r1-0528", 3000, 1200),
        ]
        for agent, model, in_t, out_t in test_cases:
            allowed = tracker.check_allowed(agent, model, in_t + out_t)
            tracker.record(agent, model, in_t, out_t)
            print(
                f"  {agent:<20} {model:<25} in={in_t} out={out_t} {'✅' if allowed else '❌'}"
            )

        print("\nBudget status:")
        for b in tracker.budget_status():
            icon = {
                "ok": "🟢",
                "warning": "🟡",
                "critical": "🔴",
                "exceeded": "💀",
            }.get(b["status"], "?")
            print(
                f"  {icon} {b['scope']:<12} {b['key']:<25} "
                f"tokens={b['usage']['total_tokens']:>8}/{b['max_tokens']:<8} "
                f"({b['token_pct']:.1f}%) "
                f"cost=${b['usage']['cost_usd']:.4f}"
            )

        print(f"\nStats: {json.dumps(tracker.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(tracker.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
