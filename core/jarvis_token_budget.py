#!/usr/bin/env python3
"""
jarvis_token_budget — Daily/hourly token spend tracking across all LLM backends
Enforces budgets, alerts on overrun, tracks cost per agent/model/node
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.token_budget")

REDIS_PREFIX = "jarvis:tokbudget:"
STATS_FILE = Path("/home/turbo/IA/Core/jarvis/data/token_budget.json")

# Budget limits (tokens per day)
DAILY_BUDGETS: dict[str, int] = {
    "claude": 50_000,  # Anthropic API — billed
    "openai": 100_000,  # OpenAI API — billed
    "local_m1": 10_000_000,  # Local — free
    "local_m2": 10_000_000,  # Local — free
    "total_billed": 150_000,  # Combined billed budget
}

# Cost per 1M tokens (USD) — approximate
COST_PER_1M: dict[str, float] = {
    "claude": 15.0,  # Sonnet/Opus mix estimate
    "openai": 10.0,
    "local_m1": 0.0,
    "local_m2": 0.0,
}


def _day_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _hour_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")


@dataclass
class SpendEntry:
    backend: str
    agent: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    ts: float = field(default_factory=time.time)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def cost_usd(self) -> float:
        rate = COST_PER_1M.get(self.backend, 0.0)
        return self.total_tokens / 1_000_000 * rate


class TokenBudgetTracker:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._local_spend: dict[str, list[SpendEntry]] = {}  # day_key -> entries
        STATS_FILE.parent.mkdir(parents=True, exist_ok=True)

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    async def record(
        self,
        backend: str,
        agent: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> dict:
        entry = SpendEntry(
            backend=backend,
            agent=agent,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

        day = _day_key()
        hour = _hour_key()

        # Local buffer
        self._local_spend.setdefault(day, []).append(entry)

        if self.redis:
            # Increment counters
            pipe = self.redis.pipeline()
            pipe.hincrby(
                f"{REDIS_PREFIX}day:{day}", f"{backend}:tokens", entry.total_tokens
            )
            pipe.hincrby(f"{REDIS_PREFIX}day:{day}", f"{backend}:prompt", prompt_tokens)
            pipe.hincrby(
                f"{REDIS_PREFIX}day:{day}", f"{backend}:completion", completion_tokens
            )
            pipe.hincrby(
                f"{REDIS_PREFIX}day:{day}", f"agent:{agent}:tokens", entry.total_tokens
            )
            pipe.hincrby(f"{REDIS_PREFIX}hour:{hour}", backend, entry.total_tokens)
            pipe.expire(f"{REDIS_PREFIX}day:{day}", 86400 * 7)
            pipe.expire(f"{REDIS_PREFIX}hour:{hour}", 86400)
            await pipe.execute()

        # Check budget
        daily_total = await self.daily_spend(backend)
        budget = DAILY_BUDGETS.get(backend)
        alert = None

        if budget and daily_total > budget * 0.9:
            alert = f"BUDGET WARNING: {backend} at {daily_total}/{budget} tokens ({daily_total / budget * 100:.0f}%)"
            log.warning(alert)
            if self.redis:
                await self.redis.publish(
                    "jarvis:events",
                    json.dumps(
                        {
                            "event": "budget_warning",
                            "backend": backend,
                            "used": daily_total,
                            "limit": budget,
                            "pct": round(daily_total / budget * 100, 1),
                        }
                    ),
                )

        return {
            "recorded": entry.total_tokens,
            "daily_total": daily_total,
            "cost_usd": round(entry.cost_usd, 6),
            "alert": alert,
        }

    async def daily_spend(self, backend: str) -> int:
        day = _day_key()
        if self.redis:
            val = await self.redis.hget(f"{REDIS_PREFIX}day:{day}", f"{backend}:tokens")
            return int(val or 0)
        # Local fallback
        entries = self._local_spend.get(day, [])
        return sum(e.total_tokens for e in entries if e.backend == backend)

    async def daily_report(self) -> dict:
        day = _day_key()
        result: dict = {
            "date": day,
            "backends": {},
            "total_cost_usd": 0.0,
            "total_tokens": 0,
        }

        if self.redis:
            raw = await self.redis.hgetall(f"{REDIS_PREFIX}day:{day}")
            for key, val in raw.items():
                if ":tokens" in key:
                    backend = key.split(":")[0]
                    tokens = int(val)
                    budget = DAILY_BUDGETS.get(backend)
                    cost = tokens / 1_000_000 * COST_PER_1M.get(backend, 0)
                    result["backends"][backend] = {
                        "tokens": tokens,
                        "budget": budget,
                        "pct": round(tokens / budget * 100, 1) if budget else None,
                        "cost_usd": round(cost, 4),
                    }
                    result["total_tokens"] += tokens
                    result["total_cost_usd"] += cost
        else:
            entries = self._local_spend.get(day, [])
            by_backend: dict[str, int] = {}
            for e in entries:
                by_backend[e.backend] = by_backend.get(e.backend, 0) + e.total_tokens
            for backend, tokens in by_backend.items():
                result["backends"][backend] = {"tokens": tokens}
                result["total_tokens"] += tokens

        result["total_cost_usd"] = round(result["total_cost_usd"], 4)
        return result

    async def hourly_breakdown(self, backend: str = "claude") -> list[dict]:
        if not self.redis:
            return []
        now = datetime.now(timezone.utc)
        breakdown = []
        for h in range(24):
            hour = now.replace(hour=h, minute=0, second=0, microsecond=0)
            key = f"{REDIS_PREFIX}hour:{hour.strftime('%Y-%m-%dT%H')}"
            val = await self.redis.hget(key, backend)
            breakdown.append(
                {
                    "hour": hour.strftime("%H:00"),
                    "tokens": int(val or 0),
                }
            )
        return breakdown

    async def agent_breakdown(self) -> list[dict]:
        day = _day_key()
        if not self.redis:
            return []
        raw = await self.redis.hgetall(f"{REDIS_PREFIX}day:{day}")
        agents = []
        for key, val in raw.items():
            if key.startswith("agent:") and key.endswith(":tokens"):
                agent = key.split(":")[1]
                agents.append({"agent": agent, "tokens": int(val)})
        return sorted(agents, key=lambda x: -x["tokens"])


# ── CLI ───────────────────────────────────────────────────────────────────────


async def main():
    import sys

    tracker = TokenBudgetTracker()
    await tracker.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"

    if cmd == "report":
        r = await tracker.daily_report()
        print(f"Token Budget — {r['date']}")
        print(f"Total: {r['total_tokens']:,} tokens | ${r['total_cost_usd']:.4f}\n")
        print(f"{'Backend':<16} {'Tokens':>10} {'Budget':>10} {'%':>6} {'Cost':>8}")
        print("-" * 56)
        for be, info in r["backends"].items():
            pct = f"{info['pct']:.1f}%" if info.get("pct") is not None else "∞"
            budget = f"{info['budget']:,}" if info.get("budget") else "∞"
            print(
                f"{be:<16} {info['tokens']:>10,} {budget:>10} {pct:>6} "
                f"${info.get('cost_usd', 0):>7.4f}"
            )

    elif cmd == "agents":
        agents = await tracker.agent_breakdown()
        print(f"{'Agent':<24} {'Tokens':>10}")
        print("-" * 36)
        for a in agents[:20]:
            print(f"{a['agent']:<24} {a['tokens']:>10,}")

    elif cmd == "record" and len(sys.argv) >= 6:
        result = await tracker.record(
            backend=sys.argv[2],
            agent=sys.argv[3],
            model=sys.argv[4],
            prompt_tokens=int(sys.argv[5]),
            completion_tokens=int(sys.argv[6]) if len(sys.argv) > 6 else 0,
        )
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
