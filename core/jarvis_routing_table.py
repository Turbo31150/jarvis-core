#!/usr/bin/env python3
"""
jarvis_routing_table — Dynamic request routing table for cluster traffic
Pattern-based routing, weighted round-robin, sticky sessions, failover
"""

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.routing_table")

REDIS_PREFIX = "jarvis:routing:"


class RoutingStrategy(str, Enum):
    ROUND_ROBIN = "round_robin"
    WEIGHTED = "weighted"
    LEAST_CONN = "least_conn"
    HASH = "hash"  # consistent hash on key
    STICKY = "sticky"  # session affinity
    FAILOVER = "failover"  # primary → fallback chain


class RouteMatchType(str, Enum):
    PREFIX = "prefix"
    SUFFIX = "suffix"
    EXACT = "exact"
    CONTAINS = "contains"
    TAG = "tag"
    ALWAYS = "always"


@dataclass
class RouteTarget:
    target_id: str
    endpoint_url: str
    weight: float = 1.0
    max_conn: int = 100
    active_conn: int = 0
    healthy: bool = True
    failure_count: int = 0
    last_used: float = 0.0
    tags: list[str] = field(default_factory=list)

    @property
    def load_score(self) -> float:
        """Lower is better."""
        if not self.healthy:
            return float("inf")
        return self.active_conn / max(self.weight, 0.01)

    def to_dict(self) -> dict:
        return {
            "target_id": self.target_id,
            "endpoint_url": self.endpoint_url,
            "weight": self.weight,
            "active_conn": self.active_conn,
            "healthy": self.healthy,
            "failure_count": self.failure_count,
            "tags": self.tags,
        }


@dataclass
class RouteRule:
    rule_id: str
    match_type: RouteMatchType
    match_value: str  # pattern to match against request key
    targets: list[RouteTarget] = field(default_factory=list)
    strategy: RoutingStrategy = RoutingStrategy.WEIGHTED
    priority: int = 0  # higher = checked first
    sticky_ttl_s: float = 300.0
    metadata: dict = field(default_factory=dict)
    _rr_idx: int = field(default=0, init=False, repr=False)

    def matches(self, key: str, tags: list[str] | None = None) -> bool:
        if self.match_type == RouteMatchType.ALWAYS:
            return True
        elif self.match_type == RouteMatchType.PREFIX:
            return key.startswith(self.match_value)
        elif self.match_type == RouteMatchType.SUFFIX:
            return key.endswith(self.match_value)
        elif self.match_type == RouteMatchType.EXACT:
            return key == self.match_value
        elif self.match_type == RouteMatchType.CONTAINS:
            return self.match_value in key
        elif self.match_type == RouteMatchType.TAG:
            return bool(tags and self.match_value in tags)
        return False

    def healthy_targets(self) -> list[RouteTarget]:
        return [t for t in self.targets if t.healthy]

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "match_type": self.match_type.value,
            "match_value": self.match_value,
            "strategy": self.strategy.value,
            "priority": self.priority,
            "targets": [t.to_dict() for t in self.targets],
        }


@dataclass
class RoutingDecision:
    target: RouteTarget | None
    rule_id: str
    strategy: RoutingStrategy
    reason: str = ""

    @property
    def resolved(self) -> bool:
        return self.target is not None

    def to_dict(self) -> dict:
        return {
            "target": self.target.to_dict() if self.target else None,
            "rule_id": self.rule_id,
            "strategy": self.strategy.value,
            "reason": self.reason,
        }


class RoutingTable:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._rules: list[RouteRule] = []
        self._sticky: dict[
            str, tuple[str, float]
        ] = {}  # session_key → (target_id, expires)
        self._stats: dict[str, int] = {
            "resolved": 0,
            "unresolved": 0,
            "sticky_hits": 0,
            "failovers": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def add_rule(self, rule: RouteRule):
        self._rules.append(rule)
        self._rules.sort(key=lambda r: -r.priority)

    def _select_weighted(self, targets: list[RouteTarget]) -> RouteTarget | None:
        if not targets:
            return None
        total_weight = sum(t.weight for t in targets)
        if total_weight <= 0:
            return targets[0]
        import random

        r = random.random() * total_weight
        cumulative = 0.0
        for t in targets:
            cumulative += t.weight
            if r <= cumulative:
                return t
        return targets[-1]

    def _select_round_robin(self, rule: RouteRule) -> RouteTarget | None:
        healthy = rule.healthy_targets()
        if not healthy:
            return None
        target = healthy[rule._rr_idx % len(healthy)]
        rule._rr_idx += 1
        return target

    def _select_least_conn(self, targets: list[RouteTarget]) -> RouteTarget | None:
        if not targets:
            return None
        return min(targets, key=lambda t: t.load_score)

    def _select_hash(self, targets: list[RouteTarget], key: str) -> RouteTarget | None:
        if not targets:
            return None
        h = int(hashlib.md5(key.encode()).hexdigest(), 16)
        return targets[h % len(targets)]

    def _select_failover(self, targets: list[RouteTarget]) -> RouteTarget | None:
        # Primary is first healthy target in order
        for t in targets:
            if t.healthy:
                return t
        return None

    def _select(
        self, rule: RouteRule, key: str, session_key: str | None
    ) -> RouteTarget | None:
        healthy = rule.healthy_targets()
        if not healthy:
            return None

        if rule.strategy == RoutingStrategy.STICKY and session_key:
            entry = self._sticky.get(session_key)
            if entry:
                tid, exp = entry
                if time.time() < exp:
                    target = next((t for t in healthy if t.target_id == tid), None)
                    if target:
                        self._stats["sticky_hits"] += 1
                        return target

        if rule.strategy == RoutingStrategy.ROUND_ROBIN:
            return self._select_round_robin(rule)
        elif rule.strategy == RoutingStrategy.WEIGHTED:
            return self._select_weighted(healthy)
        elif rule.strategy == RoutingStrategy.LEAST_CONN:
            return self._select_least_conn(healthy)
        elif rule.strategy == RoutingStrategy.HASH:
            return self._select_hash(healthy, key)
        elif rule.strategy == RoutingStrategy.FAILOVER:
            t = self._select_failover(rule.targets)
            if t and not t.healthy:
                self._stats["failovers"] += 1
            return t
        elif rule.strategy == RoutingStrategy.STICKY:
            t = self._select_weighted(healthy)
            if t and session_key:
                self._sticky[session_key] = (
                    t.target_id,
                    time.time() + rule.sticky_ttl_s,
                )
            return t
        return self._select_weighted(healthy)

    def route(
        self,
        key: str,
        tags: list[str] | None = None,
        session_key: str | None = None,
    ) -> RoutingDecision:
        for rule in self._rules:
            if rule.matches(key, tags):
                target = self._select(rule, key, session_key)
                if target:
                    target.last_used = time.time()
                    target.active_conn += 1
                    self._stats["resolved"] += 1
                    return RoutingDecision(
                        target, rule.rule_id, rule.strategy, "matched"
                    )
        self._stats["unresolved"] += 1
        return RoutingDecision(None, "", RoutingStrategy.ROUND_ROBIN, "no_rule_matched")

    def release(self, target_id: str):
        for rule in self._rules:
            for t in rule.targets:
                if t.target_id == target_id:
                    t.active_conn = max(0, t.active_conn - 1)
                    return

    def mark_healthy(self, target_id: str, healthy: bool):
        for rule in self._rules:
            for t in rule.targets:
                if t.target_id == target_id:
                    if not healthy:
                        t.failure_count += 1
                    else:
                        t.failure_count = 0
                    t.healthy = healthy
                    log.info(f"Target '{target_id}' healthy={healthy}")

    def purge_sticky(self):
        now = time.time()
        expired = [k for k, (_, exp) in self._sticky.items() if now > exp]
        for k in expired:
            del self._sticky[k]

    def list_rules(self) -> list[dict]:
        return [r.to_dict() for r in self._rules]

    def stats(self) -> dict:
        total = self._stats["resolved"] + self._stats["unresolved"]
        return {
            **self._stats,
            "total": total,
            "resolve_rate": round(self._stats["resolved"] / max(total, 1), 3),
            "rules": len(self._rules),
            "sticky_sessions": len(self._sticky),
        }


def build_jarvis_routing_table() -> RoutingTable:
    rt = RoutingTable()

    m1 = RouteTarget(
        "m1", "http://192.168.1.85:1234", weight=3.0, tags=["fast", "qwen"]
    )
    m2 = RouteTarget("m2", "http://192.168.1.26:1234", weight=2.0, tags=["deepseek"])
    ol1 = RouteTarget(
        "ol1", "http://127.0.0.1:11434", weight=1.0, tags=["local", "ollama"]
    )

    # Reasoning tasks → M2 deepseek
    rt.add_rule(
        RouteRule(
            "rule-reasoning",
            RouteMatchType.TAG,
            "reasoning",
            targets=[m2, m1],
            strategy=RoutingStrategy.FAILOVER,
            priority=10,
        )
    )
    # Local-only tasks → OL1
    rt.add_rule(
        RouteRule(
            "rule-local",
            RouteMatchType.TAG,
            "local",
            targets=[ol1],
            strategy=RoutingStrategy.ROUND_ROBIN,
            priority=9,
        )
    )
    # Default: weighted across M1/M2
    rt.add_rule(
        RouteRule(
            "rule-default",
            RouteMatchType.ALWAYS,
            "",
            targets=[m1, m2],
            strategy=RoutingStrategy.WEIGHTED,
            priority=0,
        )
    )

    return rt


async def main():
    import sys

    rt = build_jarvis_routing_table()
    await rt.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        requests = [
            ("task-001", ["reasoning"], None),
            ("task-002", ["fast"], None),
            ("task-003", ["local"], None),
            ("task-004", [], "session-abc"),
            ("task-005", [], "session-abc"),  # sticky
            ("task-006", ["reasoning"], None),
        ]

        print("Routing decisions:")
        for key, tags, sess in requests:
            d = rt.route(key, tags=tags, session_key=sess)
            icon = "✅" if d.resolved else "❌"
            target_info = (
                f"{d.target.target_id}@{d.target.endpoint_url.split('//')[-1]}"
                if d.target
                else "UNRESOLVED"
            )
            print(
                f"  {icon} {key:<10} tags={str(tags):<20} "
                f"→ {target_info:<30} rule={d.rule_id} ({d.strategy.value})"
            )
            if d.target:
                rt.release(d.target.target_id)

        print("\nRules:")
        for r in rt.list_rules():
            print(
                f"  [{r['rule_id']}] {r['match_type']}:{r['match_value']!r:<15} "
                f"strategy={r['strategy']} targets={[t['target_id'] for t in r['targets']]}"
            )

        print(f"\nStats: {json.dumps(rt.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(rt.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
