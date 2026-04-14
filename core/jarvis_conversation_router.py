#!/usr/bin/env python3
"""
jarvis_conversation_router — Routes conversations to specialized agents/models
Classifies intent, selects optimal model, maintains conversation context routing
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.conversation_router")

REDIS_PREFIX = "jarvis:conv_router:"
LM_URL = "http://127.0.0.1:1234"


@dataclass
class RoutingRule:
    name: str
    patterns: list[str]  # regex patterns to match against user message
    keywords: list[str]
    target_model: str
    agent: str = ""  # optional: dispatch to agent instead
    priority: int = 5  # 1=highest
    system_override: str = ""  # replace system prompt for this route

    def matches(self, text: str) -> float:
        """Return match score 0-1."""
        text_lower = text.lower()
        score = 0.0
        for kw in self.keywords:
            if kw.lower() in text_lower:
                score += 0.3
        for pat in self.patterns:
            if re.search(pat, text, re.IGNORECASE):
                score += 0.5
        return min(1.0, score)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "target_model": self.target_model,
            "agent": self.agent,
            "priority": self.priority,
            "keywords": self.keywords,
        }


# Built-in routing rules
DEFAULT_RULES = [
    RoutingRule(
        name="code",
        patterns=[
            r"(write|generate|fix|debug|refactor)\s+(a\s+)?(python|js|rust|go|code|function|script)",
            r"```",
            r"def\s+\w+",
            r"class\s+\w+",
        ],
        keywords=[
            "code",
            "function",
            "bug",
            "error",
            "syntax",
            "import",
            "async",
            "await",
        ],
        target_model="qwen/qwen3.5-9b",
        priority=2,
    ),
    RoutingRule(
        name="reasoning",
        patterns=[
            r"why\s+(is|does|would|should)",
            r"explain\s+(why|how)",
            r"step.by.step",
        ],
        keywords=[
            "reason",
            "logic",
            "explain",
            "analyze",
            "because",
            "therefore",
            "prove",
        ],
        target_model="deepseek/deepseek-r1-0528",
        priority=3,
    ),
    RoutingRule(
        name="trading",
        patterns=[r"(btc|eth|crypto|stock|market|price|trade|signal|chart)"],
        keywords=[
            "trading",
            "crypto",
            "bitcoin",
            "market",
            "buy",
            "sell",
            "signal",
            "position",
        ],
        target_model="qwen/qwen3.5-27b-claude-4.6-opus-distilled",
        agent="omega-trading-agent",
        priority=2,
    ),
    RoutingRule(
        name="system_ops",
        patterns=[r"(gpu|vram|cpu|ram|cluster|node|service|redis|docker)"],
        keywords=[
            "gpu",
            "temperature",
            "memory",
            "cluster",
            "deploy",
            "service",
            "status",
        ],
        target_model="qwen/qwen3.5-9b",
        agent="jarvis-system-agent",
        priority=3,
    ),
    RoutingRule(
        name="creative",
        patterns=[
            r"(write|create|generate)\s+(a\s+)?(story|poem|article|blog|email|post)"
        ],
        keywords=["creative", "write", "story", "poem", "blog", "linkedin", "content"],
        target_model="qwen/qwen3.5-27b-claude-4.6-opus-distilled",
        priority=4,
    ),
    RoutingRule(
        name="quick_qa",
        patterns=[r"^(what|who|when|where|is|are|does)\s", r"^(how\s+many|how\s+much)"],
        keywords=[],
        target_model="qwen/qwen3.5-9b",
        priority=8,
    ),
]


@dataclass
class RoutingDecision:
    rule_name: str
    target_model: str
    agent: str
    score: float
    system_override: str
    latency_ms: float = 0.0
    fallback: bool = False

    def to_dict(self) -> dict:
        return {
            "rule": self.rule_name,
            "model": self.target_model,
            "agent": self.agent,
            "score": round(self.score, 3),
            "fallback": self.fallback,
        }


class ConversationRouter:
    def __init__(self, default_model: str = "qwen/qwen3.5-9b"):
        self.default_model = default_model
        self.redis: aioredis.Redis | None = None
        self._rules: list[RoutingRule] = list(DEFAULT_RULES)
        self._decision_counts: dict[str, int] = {}

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def add_rule(self, rule: RoutingRule):
        self._rules.append(rule)
        self._rules.sort(key=lambda r: r.priority)

    def route(self, message: str, context: list[dict] | None = None) -> RoutingDecision:
        """Select best routing rule for a message."""
        t0 = time.time()
        best_rule = None
        best_score = 0.0

        for rule in sorted(self._rules, key=lambda r: r.priority):
            score = rule.matches(message)
            # Priority boost for earlier rules
            adjusted = score * (1.0 - rule.priority * 0.02)
            if adjusted > best_score:
                best_score = adjusted
                best_rule = rule

        latency_ms = (time.time() - t0) * 1000

        if best_rule and best_score > 0.2:
            decision = RoutingDecision(
                rule_name=best_rule.name,
                target_model=best_rule.target_model,
                agent=best_rule.agent,
                score=best_score,
                system_override=best_rule.system_override,
                latency_ms=latency_ms,
            )
        else:
            decision = RoutingDecision(
                rule_name="default",
                target_model=self.default_model,
                agent="",
                score=0.0,
                system_override="",
                latency_ms=latency_ms,
                fallback=True,
            )

        self._decision_counts[decision.rule_name] = (
            self._decision_counts.get(decision.rule_name, 0) + 1
        )
        log.debug(
            f"Route: '{message[:50]}' → {decision.rule_name} ({decision.target_model}) score={best_score:.2f}"
        )
        return decision

    async def route_and_infer(
        self,
        messages: list[dict],
        session_id: str = "",
        max_tokens: int = 1024,
    ) -> tuple[str, RoutingDecision]:
        """Route and execute inference."""
        last_user = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "user"), ""
        )
        decision = self.route(last_user)

        # Apply system override if needed
        msgs = list(messages)
        if decision.system_override:
            sys_idx = next(
                (i for i, m in enumerate(msgs) if m.get("role") == "system"), None
            )
            if sys_idx is not None:
                msgs[sys_idx] = {"role": "system", "content": decision.system_override}
            else:
                msgs.insert(0, {"role": "system", "content": decision.system_override})

        if self.redis:
            await self.redis.hincrby(f"{REDIS_PREFIX}routes", decision.rule_name, 1)

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=90)
            ) as sess:
                async with sess.post(
                    f"{LM_URL}/v1/chat/completions",
                    json={
                        "model": decision.target_model,
                        "messages": msgs,
                        "max_tokens": max_tokens,
                        "temperature": 0.7,
                    },
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        return data["choices"][0]["message"][
                            "content"
                        ].strip(), decision
        except Exception as e:
            log.error(f"Infer error: {e}")

        return "", decision

    def stats(self) -> dict:
        total = sum(self._decision_counts.values())
        return {
            "total_routed": total,
            "by_rule": sorted(self._decision_counts.items(), key=lambda x: -x[1]),
            "rules": len(self._rules),
        }


async def main():
    import sys

    router = ConversationRouter()
    await router.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        test_msgs = [
            "Write a Python function to parse JSON",
            "What is the BTC price today?",
            "Explain why transformers work better than RNNs",
            "How many GPUs does M1 have?",
            "What is 2+2?",
            "Write a blog post about async Python",
        ]
        print(f"{'Message':<45} {'Rule':<14} {'Model':<30} {'Score'}")
        print("-" * 95)
        for msg in test_msgs:
            d = router.route(msg)
            print(
                f"  {msg[:43]:<45} {d.rule_name:<14} {d.target_model:<30} {d.score:.2f}"
            )

    elif cmd == "route" and len(sys.argv) > 2:
        msg = " ".join(sys.argv[2:])
        d = router.route(msg)
        print(json.dumps(d.to_dict(), indent=2))

    elif cmd == "stats":
        print(json.dumps(router.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
