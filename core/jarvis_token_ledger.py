#!/usr/bin/env python3
"""
jarvis_token_ledger — Token usage tracking and budget enforcement
Per-user/model token accounting with daily/monthly limits and cost projection
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.token_ledger")

REDIS_PREFIX = "jarvis:ledger:"
LEDGER_FILE = Path("/home/turbo/IA/Core/jarvis/data/token_ledger.jsonl")

# Cost per 1M tokens (approximate, for local models = 0, but track for comparisons)
COST_PER_1M: dict[str, dict] = {
    "qwen/qwen3.5-9b": {"input": 0.0, "output": 0.0},
    "qwen/qwen3.5-27b-claude": {"input": 0.0, "output": 0.0},
    "qwen/qwen3.5-35b-a3b": {"input": 0.0, "output": 0.0},
    "deepseek-r1-0528": {"input": 0.0, "output": 0.0},
    "claude-opus-4": {"input": 15.0, "output": 75.0},
    "claude-sonnet-4": {"input": 3.0, "output": 15.0},
    "claude-haiku-4": {"input": 0.8, "output": 4.0},
    "gpt-4o": {"input": 5.0, "output": 15.0},
    "gpt-4o-mini": {"input": 0.15, "output": 0.6},
    "default": {"input": 0.0, "output": 0.0},
}


@dataclass
class TokenUsage:
    model: str
    user: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    ts: float = field(default_factory=time.time)
    request_id: str = ""
    node: str = ""

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "user": self.user,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "ts": self.ts,
            "request_id": self.request_id,
        }


@dataclass
class UserBudget:
    user: str
    daily_token_limit: int = 1_000_000
    monthly_token_limit: int = 30_000_000
    daily_cost_limit_usd: float = 0.0  # 0 = unlimited
    monthly_cost_limit_usd: float = 0.0


@dataclass
class LedgerSummary:
    user: str
    period: str
    total_input: int
    total_output: int
    total_tokens: int
    total_cost_usd: float
    request_count: int
    by_model: dict[str, dict]

    def to_dict(self) -> dict:
        return {
            "user": self.user,
            "period": self.period,
            "total_input": self.total_input,
            "total_output": self.total_output,
            "total_tokens": self.total_tokens,
            "total_cost_usd": round(self.total_cost_usd, 4),
            "request_count": self.request_count,
            "by_model": self.by_model,
        }


class TokenLedger:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._entries: list[TokenUsage] = []
        self._budgets: dict[str, UserBudget] = {}
        LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def set_budget(self, user: str, **kwargs) -> UserBudget:
        budget = UserBudget(user=user, **kwargs)
        self._budgets[user] = budget
        return budget

    def _compute_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        costs = COST_PER_1M.get(model, COST_PER_1M["default"])
        return (
            input_tokens * costs["input"] + output_tokens * costs["output"]
        ) / 1_000_000

    def record(
        self,
        model: str,
        user: str,
        input_tokens: int,
        output_tokens: int,
        request_id: str = "",
        node: str = "",
    ) -> TokenUsage:
        cost = self._compute_cost(model, input_tokens, output_tokens)
        entry = TokenUsage(
            model=model,
            user=user,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            request_id=request_id,
            node=node,
        )
        self._entries.append(entry)
        self._persist(entry)

        if self.redis:
            asyncio.create_task(self._push_redis(entry))

        return entry

    def _persist(self, entry: TokenUsage):
        with open(LEDGER_FILE, "a") as f:
            f.write(json.dumps(entry.to_dict()) + "\n")

    async def _push_redis(self, entry: TokenUsage):
        day_key = time.strftime("%Y-%m-%d", time.localtime(entry.ts))
        # Per-user daily totals
        await self.redis.hincrby(
            f"{REDIS_PREFIX}daily:{entry.user}:{day_key}",
            "total_tokens",
            entry.total_tokens,
        )
        await self.redis.hincrby(
            f"{REDIS_PREFIX}daily:{entry.user}:{day_key}",
            "input_tokens",
            entry.input_tokens,
        )
        await self.redis.hincrby(
            f"{REDIS_PREFIX}daily:{entry.user}:{day_key}",
            "output_tokens",
            entry.output_tokens,
        )
        await self.redis.expire(
            f"{REDIS_PREFIX}daily:{entry.user}:{day_key}", 86400 * 32
        )

    def check_budget(self, user: str, estimated_tokens: int = 0) -> tuple[bool, str]:
        """Returns (allowed, reason). True if within budget."""
        budget = self._budgets.get(user)
        if not budget:
            return True, "no budget set"

        now = time.time()
        day_start = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
        month_start = time.mktime(time.strptime(time.strftime("%Y-%m-01"), "%Y-%m-%d"))

        daily_entries = [
            e for e in self._entries if e.user == user and e.ts >= day_start
        ]
        monthly_entries = [
            e for e in self._entries if e.user == user and e.ts >= month_start
        ]

        daily_tokens = sum(e.total_tokens for e in daily_entries) + estimated_tokens
        monthly_tokens = sum(e.total_tokens for e in monthly_entries) + estimated_tokens
        daily_cost = sum(e.cost_usd for e in daily_entries)
        monthly_cost = sum(e.cost_usd for e in monthly_entries)

        if daily_tokens > budget.daily_token_limit:
            return (
                False,
                f"Daily token limit exceeded: {daily_tokens}/{budget.daily_token_limit}",
            )
        if monthly_tokens > budget.monthly_token_limit:
            return (
                False,
                f"Monthly token limit exceeded: {monthly_tokens}/{budget.monthly_token_limit}",
            )
        if budget.daily_cost_limit_usd and daily_cost > budget.daily_cost_limit_usd:
            return (
                False,
                f"Daily cost limit exceeded: ${daily_cost:.2f}/${budget.daily_cost_limit_usd:.2f}",
            )
        if (
            budget.monthly_cost_limit_usd
            and monthly_cost > budget.monthly_cost_limit_usd
        ):
            return (
                False,
                f"Monthly cost limit exceeded: ${monthly_cost:.2f}/${budget.monthly_cost_limit_usd:.2f}",
            )

        return True, "ok"

    def summarize(self, user: str, period: str = "today") -> LedgerSummary:
        now = time.time()
        if period == "today":
            cutoff = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
        elif period == "month":
            cutoff = time.mktime(time.strptime(time.strftime("%Y-%m-01"), "%Y-%m-%d"))
        elif period == "all":
            cutoff = 0.0
        else:
            cutoff = now - 3600  # last hour

        entries = [e for e in self._entries if e.user == user and e.ts >= cutoff]
        by_model: dict[str, dict] = {}
        for e in entries:
            if e.model not in by_model:
                by_model[e.model] = {"requests": 0, "tokens": 0, "cost_usd": 0.0}
            by_model[e.model]["requests"] += 1
            by_model[e.model]["tokens"] += e.total_tokens
            by_model[e.model]["cost_usd"] = round(
                by_model[e.model]["cost_usd"] + e.cost_usd, 6
            )

        return LedgerSummary(
            user=user,
            period=period,
            total_input=sum(e.input_tokens for e in entries),
            total_output=sum(e.output_tokens for e in entries),
            total_tokens=sum(e.total_tokens for e in entries),
            total_cost_usd=sum(e.cost_usd for e in entries),
            request_count=len(entries),
            by_model=by_model,
        )

    def global_stats(self, period: str = "today") -> dict:
        users = {e.user for e in self._entries}
        summaries = [self.summarize(u, period) for u in users]
        return {
            "period": period,
            "users": len(users),
            "total_tokens": sum(s.total_tokens for s in summaries),
            "total_cost_usd": round(sum(s.total_cost_usd for s in summaries), 4),
            "total_requests": sum(s.request_count for s in summaries),
        }


async def main():
    import sys

    ledger = TokenLedger()
    await ledger.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        ledger.set_budget("turbo", daily_token_limit=500_000)
        ledger.set_budget(
            "api_user", daily_token_limit=100_000, daily_cost_limit_usd=1.0
        )

        models = [
            ("qwen/qwen3.5-9b", "turbo", 150, 300),
            ("qwen/qwen3.5-27b-claude", "turbo", 200, 450),
            ("claude-sonnet-4", "api_user", 500, 1000),
            ("gpt-4o", "api_user", 300, 600),
            ("claude-opus-4", "turbo", 100, 200),
        ]
        for model, user, inp, out in models:
            entry = ledger.record(model, user, inp, out)
            print(
                f"  [{user}] {model}: {entry.total_tokens} tokens ${entry.cost_usd:.4f}"
            )

        print()
        for user in ["turbo", "api_user"]:
            summary = ledger.summarize(user, "today")
            print(
                f"  {user}: {summary.total_tokens} tokens, ${summary.total_cost_usd:.4f}, {summary.request_count} requests"
            )
            for model, stats in summary.by_model.items():
                print(
                    f"    {model.split('/')[-1]}: {stats['tokens']} tok ${stats['cost_usd']:.4f}"
                )

        ok, reason = ledger.check_budget("turbo")
        print(f"\nBudget check turbo: {'✅' if ok else '❌'} {reason}")

    elif cmd == "stats":
        print(json.dumps(ledger.global_stats("today"), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
