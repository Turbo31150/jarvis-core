#!/usr/bin/env python3
"""
jarvis_cost_optimizer — Route requests to cheapest capable backend
Scores backends by cost/quality, enforces daily budgets, maximizes local usage
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.cost_optimizer")

REDIS_KEY = "jarvis:cost_optimizer"

# Cost per 1M tokens (USD) — 0 for local
BACKEND_COSTS: dict[str, dict] = {
    "M1": {
        "url": "http://127.0.0.1:1234",
        "cost_per_1m": 0.0,
        "quality": 0.80,
        "priority": 1,
    },
    "M2": {
        "url": "http://192.168.1.26:1234",
        "cost_per_1m": 0.0,
        "quality": 0.80,
        "priority": 2,
    },
    "OL1": {
        "url": "http://127.0.0.1:11434",
        "cost_per_1m": 0.0,
        "quality": 0.65,
        "priority": 3,
    },
    "Claude": {
        "url": "https://api.anthropic.com",
        "cost_per_1m": 15.0,
        "quality": 0.99,
        "priority": 10,
    },
    "OpenAI": {
        "url": "https://api.openai.com",
        "cost_per_1m": 10.0,
        "quality": 0.97,
        "priority": 9,
    },
}

# Task complexity tiers
TASK_TIERS = {
    "simple": {"min_quality": 0.60, "max_cost": 0.0},  # local only
    "standard": {"min_quality": 0.75, "max_cost": 0.001},  # local preferred
    "complex": {"min_quality": 0.85, "max_cost": 0.01},  # allow paid if needed
    "critical": {"min_quality": 0.95, "max_cost": 1.0},  # best available
}

DAILY_BUDGET_USD = 0.50  # hard daily limit for paid APIs


@dataclass
class RoutingDecision:
    backend: str
    url: str
    model: str
    estimated_cost_usd: float
    quality_score: float
    reason: str
    tier: str


@dataclass
class SpendTracker:
    daily_spend: float = 0.0
    day_key: str = ""
    requests: dict[str, int] = field(default_factory=dict)

    def reset_if_new_day(self):
        today = time.strftime("%Y-%m-%d")
        if self.day_key != today:
            self.daily_spend = 0.0
            self.day_key = today
            self.requests = {}

    def record(self, backend: str, tokens: int):
        cost_rate = BACKEND_COSTS.get(backend, {}).get("cost_per_1m", 0.0)
        cost = tokens / 1_000_000 * cost_rate
        self.daily_spend += cost
        self.requests[backend] = self.requests.get(backend, 0) + 1
        return cost


class CostOptimizer:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._spend = SpendTracker()
        self._availability: dict[str, bool] = {b: True for b in BACKEND_COSTS}

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    async def _probe_backend(self, name: str) -> bool:
        cfg = BACKEND_COSTS[name]
        if cfg["url"].startswith("https://api."):
            return True  # assume external APIs up
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=3)
            ) as sess:
                async with sess.get(f"{cfg['url']}/v1/models") as r:
                    return r.status == 200
        except Exception:
            return False

    async def refresh_availability(self):
        tasks = {name: self._probe_backend(name) for name in BACKEND_COSTS}
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for name, result in zip(tasks.keys(), results):
            self._availability[name] = bool(result) and not isinstance(
                result, Exception
            )

    def route(
        self,
        task_tier: str = "standard",
        preferred_model: str | None = None,
        estimated_tokens: int = 500,
    ) -> RoutingDecision:
        self._spend.reset_if_new_day()
        tier_cfg = TASK_TIERS.get(task_tier, TASK_TIERS["standard"])
        min_quality = tier_cfg["min_quality"]
        max_cost_per_request = tier_cfg["max_cost"]

        # Filter: available + meets quality + affordable + budget not exceeded
        candidates = []
        for name, cfg in BACKEND_COSTS.items():
            if not self._availability.get(name, True):
                continue
            if cfg["quality"] < min_quality:
                continue
            est_cost = estimated_tokens / 1_000_000 * cfg["cost_per_1m"]
            if est_cost > max_cost_per_request:
                continue
            if cfg["cost_per_1m"] > 0 and self._spend.daily_spend >= DAILY_BUDGET_USD:
                continue
            candidates.append((name, cfg, est_cost))

        if not candidates:
            # Fallback to M1 regardless
            return RoutingDecision(
                backend="M1",
                url=BACKEND_COSTS["M1"]["url"],
                model=preferred_model or "qwen/qwen3.5-9b",
                estimated_cost_usd=0.0,
                quality_score=BACKEND_COSTS["M1"]["quality"],
                reason="fallback: no candidates met criteria",
                tier=task_tier,
            )

        # Score: quality_weight * quality - cost_weight * normalized_cost
        def score(item) -> float:
            name, cfg, est_cost = item
            cost_penalty = est_cost * 100  # penalize cost
            priority_bonus = 0.1 / cfg["priority"]  # lower priority = lower bonus
            return cfg["quality"] + priority_bonus - cost_penalty

        best_name, best_cfg, best_cost = max(candidates, key=score)

        # Pick model for backend
        model_map = {
            "M1": "qwen/qwen3.5-9b",
            "M2": "qwen/qwen3.5-9b",
            "OL1": "qwen2.5:1.5b",
        }
        model = preferred_model or model_map.get(best_name, "qwen/qwen3.5-9b")

        reason = f"tier={task_tier} quality={best_cfg['quality']:.2f} cost=${best_cost:.5f}/req"
        if best_cost == 0:
            reason += " (free local)"

        return RoutingDecision(
            backend=best_name,
            url=best_cfg["url"],
            model=model,
            estimated_cost_usd=best_cost,
            quality_score=best_cfg["quality"],
            reason=reason,
            tier=task_tier,
        )

    def record_usage(self, backend: str, tokens: int) -> float:
        return self._spend.record(backend, tokens)

    async def status(self) -> dict:
        await self.refresh_availability()
        self._spend.reset_if_new_day()

        result = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "daily_spend_usd": round(self._spend.daily_spend, 4),
            "daily_budget_usd": DAILY_BUDGET_USD,
            "budget_used_pct": round(
                self._spend.daily_spend / DAILY_BUDGET_USD * 100, 1
            ),
            "requests_by_backend": self._spend.requests,
            "backends": {},
        }

        for name, cfg in BACKEND_COSTS.items():
            result["backends"][name] = {
                "available": self._availability.get(name, True),
                "cost_per_1m": cfg["cost_per_1m"],
                "quality": cfg["quality"],
                "priority": cfg["priority"],
            }

        if self.redis:
            await self.redis.set(REDIS_KEY, json.dumps(result), ex=60)

        return result


async def main():
    import sys

    opt = CostOptimizer()
    await opt.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "status":
        s = await opt.status()
        print(
            f"Daily spend: ${s['daily_spend_usd']:.4f} / ${s['daily_budget_usd']} ({s['budget_used_pct']}%)\n"
        )
        print(
            f"{'Backend':<10} {'Available':>10} {'Cost/1M':>9} {'Quality':>8} {'Requests':>9}"
        )
        print("-" * 52)
        for name, info in s["backends"].items():
            avail = "✅" if info["available"] else "❌"
            reqs = s["requests_by_backend"].get(name, 0)
            cost = f"${info['cost_per_1m']:.2f}" if info["cost_per_1m"] > 0 else "free"
            print(f"{name:<10} {avail:>10} {cost:>9} {info['quality']:>8.2f} {reqs:>9}")

    elif cmd == "route":
        tier = sys.argv[2] if len(sys.argv) > 2 else "standard"
        tokens = int(sys.argv[3]) if len(sys.argv) > 3 else 500
        await opt.refresh_availability()
        decision = opt.route(task_tier=tier, estimated_tokens=tokens)
        print(f"Tier: {decision.tier}")
        print(f"Backend: {decision.backend} ({decision.url})")
        print(f"Model: {decision.model}")
        print(f"Est. cost: ${decision.estimated_cost_usd:.6f}")
        print(f"Quality: {decision.quality_score:.2f}")
        print(f"Reason: {decision.reason}")

    elif cmd == "simulate":
        await opt.refresh_availability()
        for tier in TASK_TIERS:
            d = opt.route(task_tier=tier, estimated_tokens=1000)
            print(
                f"  {tier:<10} → [{d.backend:<6}] {d.model:<30} ${d.estimated_cost_usd:.5f}"
            )


if __name__ == "__main__":
    asyncio.run(main())
