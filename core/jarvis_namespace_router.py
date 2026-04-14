#!/usr/bin/env python3
"""
jarvis_namespace_router — Multi-tenant namespace routing and isolation
Route requests to isolated namespaces with per-namespace config and quotas
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.namespace_router")

REDIS_PREFIX = "jarvis:ns:"


class NamespaceStatus(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DRAINING = "draining"
    DELETED = "deleted"


class IsolationLevel(str, Enum):
    SHARED = "shared"  # Share all resources
    SOFT = "soft"  # Soft quotas, shared pool
    HARD = "hard"  # Hard quotas, dedicated pool
    EXCLUSIVE = "exclusive"  # Fully isolated


@dataclass
class NamespaceQuota:
    max_req_per_min: int = 0  # 0 = unlimited
    max_tokens_per_day: int = 0
    max_cost_usd_per_day: float = 0.0
    max_concurrent: int = 0
    allowed_models: list[str] = field(default_factory=list)  # empty = all
    priority: int = 5  # 1=highest, 10=lowest


@dataclass
class Namespace:
    ns_id: str
    name: str
    status: NamespaceStatus = NamespaceStatus.ACTIVE
    isolation: IsolationLevel = IsolationLevel.SOFT
    quota: NamespaceQuota = field(default_factory=NamespaceQuota)
    labels: dict[str, str] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    # Runtime counters (reset periodically)
    req_count_min: int = 0
    req_window_start: float = field(default_factory=time.time)
    tokens_today: int = 0
    cost_today: float = 0.0
    active_requests: int = 0
    total_requests: int = 0

    def is_active(self) -> bool:
        return self.status == NamespaceStatus.ACTIVE

    def check_model_allowed(self, model: str) -> bool:
        if not self.quota.allowed_models:
            return True
        return any(m.lower() in model.lower() for m in self.quota.allowed_models)

    def _reset_window_if_needed(self):
        if time.time() - self.req_window_start >= 60.0:
            self.req_count_min = 0
            self.req_window_start = time.time()

    def can_accept(
        self, model: str = "", estimated_tokens: int = 0
    ) -> tuple[bool, str]:
        if not self.is_active():
            return False, f"namespace {self.ns_id} is {self.status.value}"

        if model and not self.check_model_allowed(model):
            return False, f"model {model!r} not allowed in namespace {self.ns_id}"

        q = self.quota
        self._reset_window_if_needed()

        if q.max_req_per_min > 0 and self.req_count_min >= q.max_req_per_min:
            return (
                False,
                f"rate limit: {self.req_count_min}/{q.max_req_per_min} req/min",
            )

        if q.max_concurrent > 0 and self.active_requests >= q.max_concurrent:
            return (
                False,
                f"concurrency limit: {self.active_requests}/{q.max_concurrent}",
            )

        if (
            q.max_tokens_per_day > 0
            and self.tokens_today + estimated_tokens > q.max_tokens_per_day
        ):
            return (
                False,
                f"daily token limit: {self.tokens_today}/{q.max_tokens_per_day}",
            )

        return True, ""

    def record_request(self, tokens: int = 0, cost: float = 0.0):
        self._reset_window_if_needed()
        self.req_count_min += 1
        self.total_requests += 1
        self.active_requests += 1
        self.tokens_today += tokens
        self.cost_today += cost

    def record_complete(self):
        self.active_requests = max(0, self.active_requests - 1)

    def to_dict(self) -> dict:
        return {
            "ns_id": self.ns_id,
            "name": self.name,
            "status": self.status.value,
            "isolation": self.isolation.value,
            "priority": self.quota.priority,
            "req_count_min": self.req_count_min,
            "active_requests": self.active_requests,
            "total_requests": self.total_requests,
            "tokens_today": self.tokens_today,
            "cost_today": round(self.cost_today, 6),
            "labels": self.labels,
        }


@dataclass
class RouteResult:
    ns_id: str
    allowed: bool
    reason: str = ""
    config: dict = field(default_factory=dict)


class NamespaceRouter:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._namespaces: dict[str, Namespace] = {}
        self._routing_rules: list[dict] = []  # {pattern, ns_id, priority}
        self._default_ns: str = "default"
        self._stats: dict[str, int] = {
            "routed": 0,
            "blocked": 0,
            "fallback_default": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def create_namespace(
        self,
        ns_id: str,
        name: str = "",
        isolation: IsolationLevel = IsolationLevel.SOFT,
        quota: NamespaceQuota | None = None,
        labels: dict | None = None,
        config: dict | None = None,
    ) -> Namespace:
        ns = Namespace(
            ns_id=ns_id,
            name=name or ns_id,
            isolation=isolation,
            quota=quota or NamespaceQuota(),
            labels=labels or {},
            config=config or {},
        )
        self._namespaces[ns_id] = ns
        return ns

    def add_routing_rule(self, pattern: str, ns_id: str, priority: int = 5):
        """Pattern can match agent_id prefix, label, or tag."""
        self._routing_rules.append(
            {"pattern": pattern, "ns_id": ns_id, "priority": priority}
        )
        self._routing_rules.sort(key=lambda r: r["priority"])

    def resolve(self, agent_id: str, tags: list[str] | None = None) -> str:
        """Returns the namespace ID for an agent/request."""
        tags = tags or []
        for rule in self._routing_rules:
            p = rule["pattern"]
            if p in agent_id or any(p in t for t in tags):
                return rule["ns_id"]
        return self._default_ns

    def route(
        self,
        agent_id: str,
        model: str = "",
        tags: list[str] | None = None,
        estimated_tokens: int = 0,
    ) -> RouteResult:
        ns_id = self.resolve(agent_id, tags)
        ns = self._namespaces.get(ns_id)

        if not ns:
            # Auto-create on first use
            ns = self.create_namespace(ns_id)

        allowed, reason = ns.can_accept(model, estimated_tokens)
        self._stats["routed"] += 1
        if not allowed:
            self._stats["blocked"] += 1
            log.warning(f"Namespace [{ns_id}] blocked {agent_id}: {reason}")

        if ns_id == self._default_ns:
            self._stats["fallback_default"] += 1

        return RouteResult(
            ns_id=ns_id, allowed=allowed, reason=reason, config=ns.config
        )

    def record(self, ns_id: str, tokens: int = 0, cost: float = 0.0):
        ns = self._namespaces.get(ns_id)
        if ns:
            ns.record_request(tokens, cost)

    def complete(self, ns_id: str):
        ns = self._namespaces.get(ns_id)
        if ns:
            ns.record_complete()

    def suspend(self, ns_id: str):
        ns = self._namespaces.get(ns_id)
        if ns:
            ns.status = NamespaceStatus.SUSPENDED
            log.warning(f"Namespace {ns_id} suspended")

    def activate(self, ns_id: str):
        ns = self._namespaces.get(ns_id)
        if ns:
            ns.status = NamespaceStatus.ACTIVE

    def reset_daily(self, ns_id: str | None = None):
        targets = (
            [self._namespaces[ns_id]] if ns_id else list(self._namespaces.values())
        )
        for ns in targets:
            ns.tokens_today = 0
            ns.cost_today = 0.0

    def all_namespaces(self) -> list[dict]:
        return [ns.to_dict() for ns in self._namespaces.values()]

    def stats(self) -> dict:
        return {
            **self._stats,
            "namespaces": len(self._namespaces),
            "active": sum(1 for ns in self._namespaces.values() if ns.is_active()),
        }


def build_jarvis_namespace_router() -> NamespaceRouter:
    router = NamespaceRouter()

    router.create_namespace(
        "default",
        name="Default",
        quota=NamespaceQuota(max_req_per_min=60, priority=5),
    )
    router.create_namespace(
        "trading",
        name="Trading Agents",
        isolation=IsolationLevel.HARD,
        quota=NamespaceQuota(
            max_req_per_min=30,
            max_tokens_per_day=500_000,
            max_cost_usd_per_day=1.0,
            max_concurrent=5,
            priority=2,
        ),
    )
    router.create_namespace(
        "monitoring",
        name="Monitoring",
        quota=NamespaceQuota(max_req_per_min=120, priority=8),
    )
    router.create_namespace(
        "admin",
        name="Admin",
        isolation=IsolationLevel.EXCLUSIVE,
        quota=NamespaceQuota(priority=1),
    )

    router.add_routing_rule("trading", "trading", priority=1)
    router.add_routing_rule("monitor", "monitoring", priority=2)
    router.add_routing_rule("admin", "admin", priority=1)

    return router


async def main():
    import sys

    router = build_jarvis_namespace_router()
    await router.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Routing test requests...")
        agents = [
            ("trading-agent", "qwen3.5-9b", []),
            ("trading-signal", "deepseek-r1", []),
            ("monitor-health", "gemma3:4b", []),
            ("inference-gw", "claude-sonnet-4-6", []),
            ("admin-cli", "qwen3.5-27b", ["admin"]),
        ]
        for agent_id, model, tags in agents:
            result = router.route(agent_id, model, tags)
            icon = "✅" if result.allowed else "❌"
            print(
                f"  {icon} {agent_id:<20} → ns={result.ns_id:<12} {result.reason or 'ok'}"
            )
            if result.allowed:
                router.record(result.ns_id, tokens=500)

        print("\nNamespace states:")
        for ns in router.all_namespaces():
            print(
                f"  {ns['ns_id']:<12} req/min={ns['req_count_min']} total={ns['total_requests']}"
            )

        print(f"\nStats: {json.dumps(router.stats(), indent=2)}")

    elif cmd == "list":
        for ns in router.all_namespaces():
            print(json.dumps(ns))

    elif cmd == "stats":
        print(json.dumps(router.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
