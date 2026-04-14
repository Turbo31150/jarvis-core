#!/usr/bin/env python3
"""
jarvis_node_affinity — Node affinity and anti-affinity rules for task placement
Hard/soft constraints, label selectors, topology spread, and scoring
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.node_affinity")

REDIS_PREFIX = "jarvis:affinity:"


class AffinityType(str, Enum):
    REQUIRED = "required"  # hard constraint — task must match
    PREFERRED = "preferred"  # soft constraint — score bonus if matched


class MatchOperator(str, Enum):
    IN = "in"
    NOT_IN = "not_in"
    EXISTS = "exists"
    DOES_NOT_EXIST = "does_not_exist"
    GT = "gt"
    LT = "lt"
    EQ = "eq"


@dataclass
class LabelSelector:
    key: str
    operator: MatchOperator
    values: list[str] = field(default_factory=list)
    numeric_value: float = 0.0

    def matches(self, labels: dict[str, str]) -> bool:
        val = labels.get(self.key)
        if self.operator == MatchOperator.IN:
            return val in self.values
        elif self.operator == MatchOperator.NOT_IN:
            return val not in self.values
        elif self.operator == MatchOperator.EXISTS:
            return self.key in labels
        elif self.operator == MatchOperator.DOES_NOT_EXIST:
            return self.key not in labels
        elif self.operator == MatchOperator.EQ:
            return val == (self.values[0] if self.values else "")
        elif self.operator == MatchOperator.GT:
            try:
                return float(val or 0) > self.numeric_value
            except ValueError:
                return False
        elif self.operator == MatchOperator.LT:
            try:
                return float(val or 0) < self.numeric_value
            except ValueError:
                return False
        return False


@dataclass
class AffinityRule:
    rule_id: str
    affinity_type: AffinityType
    selectors: list[LabelSelector] = field(default_factory=list)
    weight: int = 1  # for preferred rules (1–100)
    description: str = ""

    def matches_all(self, labels: dict[str, str]) -> bool:
        return all(s.matches(labels) for s in self.selectors)


@dataclass
class NodeProfile:
    node_id: str
    labels: dict[str, str] = field(default_factory=dict)
    capacity: dict[str, float] = field(default_factory=dict)
    available: dict[str, float] = field(default_factory=dict)
    taints: list[str] = field(default_factory=list)  # nodes can reject tasks
    healthy: bool = True
    last_seen: float = field(default_factory=time.time)

    @property
    def stale(self) -> bool:
        return (time.time() - self.last_seen) > 120.0

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "labels": self.labels,
            "capacity": self.capacity,
            "available": self.available,
            "taints": self.taints,
            "healthy": self.healthy,
        }


@dataclass
class PlacementResult:
    node_id: str | None
    score: float
    reason: str
    scores_by_node: dict[str, float] = field(default_factory=dict)

    @property
    def placed(self) -> bool:
        return self.node_id is not None

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "score": round(self.score, 3),
            "reason": self.reason,
            "scores_by_node": {k: round(v, 3) for k, v in self.scores_by_node.items()},
        }


@dataclass
class TaskAffinitySpec:
    task_id: str
    required_rules: list[AffinityRule] = field(default_factory=list)
    preferred_rules: list[AffinityRule] = field(default_factory=list)
    anti_affinity: list[AffinityRule] = field(default_factory=list)
    tolerate_taints: list[str] = field(default_factory=list)
    resource_request: dict[str, float] = field(default_factory=dict)


class NodeAffinityScheduler:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._nodes: dict[str, NodeProfile] = {}
        self._placed: dict[str, str] = {}  # task_id → node_id (for anti-affinity)
        self._stats: dict[str, int] = {
            "placements": 0,
            "failed_placements": 0,
            "hard_constraint_failures": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def register_node(self, node: NodeProfile):
        self._nodes[node.node_id] = node

    def update_node(
        self,
        node_id: str,
        labels: dict[str, str] | None = None,
        available: dict[str, float] | None = None,
        healthy: bool | None = None,
    ):
        node = self._nodes.get(node_id)
        if not node:
            return
        if labels:
            node.labels.update(labels)
        if available:
            node.available.update(available)
        if healthy is not None:
            node.healthy = healthy
        node.last_seen = time.time()

    def _satisfies_resources(
        self, node: NodeProfile, request: dict[str, float]
    ) -> bool:
        for res, amount in request.items():
            if node.available.get(res, 0.0) < amount:
                return False
        return True

    def _check_taints(self, node: NodeProfile, tolerate: list[str]) -> bool:
        untolerated = [t for t in node.taints if t not in tolerate]
        return len(untolerated) == 0

    def place(self, spec: TaskAffinitySpec) -> PlacementResult:
        scores_by_node: dict[str, float] = {}

        for node in self._nodes.values():
            if not node.healthy or node.stale:
                continue

            # Taint tolerance
            if not self._check_taints(node, spec.tolerate_taints):
                continue

            # Resource availability
            if not self._satisfies_resources(node, spec.resource_request):
                continue

            # Hard required rules (all must match)
            hard_ok = all(r.matches_all(node.labels) for r in spec.required_rules)
            if not hard_ok:
                self._stats["hard_constraint_failures"] += 1
                continue

            # Anti-affinity hard rules
            anti_fail = any(r.matches_all(node.labels) for r in spec.anti_affinity)
            if anti_fail:
                continue

            # Soft preferred rules: accumulate score
            pref_score = sum(
                r.weight for r in spec.preferred_rules if r.matches_all(node.labels)
            )

            # Resource utilization score (prefer less utilized nodes)
            util_penalty = 0.0
            for res, amount in spec.resource_request.items():
                cap = node.capacity.get(res, 1.0)
                avail = node.available.get(res, 0.0)
                if cap > 0:
                    util_penalty += (cap - avail) / cap
            util_score = max(0.0, 10.0 - util_penalty * 5)

            scores_by_node[node.node_id] = float(pref_score) + util_score

        if not scores_by_node:
            self._stats["failed_placements"] += 1
            return PlacementResult(None, 0.0, "No eligible node found", {})

        best_node = max(scores_by_node, key=lambda n: scores_by_node[n])
        self._placed[spec.task_id] = best_node
        self._stats["placements"] += 1

        # Deduct resources
        node = self._nodes[best_node]
        for res, amount in spec.resource_request.items():
            node.available[res] = max(0.0, node.available.get(res, 0.0) - amount)

        log.info(
            f"Placed task '{spec.task_id}' on '{best_node}' "
            f"(score={scores_by_node[best_node]:.1f})"
        )
        return PlacementResult(
            best_node, scores_by_node[best_node], "OK", scores_by_node
        )

    def release(self, task_id: str, resource_request: dict[str, float]):
        node_id = self._placed.pop(task_id, None)
        if not node_id:
            return
        node = self._nodes.get(node_id)
        if node:
            for res, amount in resource_request.items():
                node.available[res] = node.available.get(res, 0.0) + amount

    def list_nodes(self) -> list[dict]:
        return [n.to_dict() for n in self._nodes.values()]

    def stats(self) -> dict:
        return {
            **self._stats,
            "nodes": len(self._nodes),
            "placed_tasks": len(self._placed),
        }


def build_jarvis_node_affinity() -> NodeAffinityScheduler:
    sched = NodeAffinityScheduler()

    sched.register_node(
        NodeProfile(
            node_id="m1",
            labels={"gpu": "nvidia", "tier": "fast", "region": "local", "vram": "12"},
            capacity={"vram_mb": 12288, "ram_mb": 32768, "cpu": 800},
            available={"vram_mb": 10000, "ram_mb": 28000, "cpu": 600},
        )
    )
    sched.register_node(
        NodeProfile(
            node_id="m2",
            labels={"gpu": "nvidia", "tier": "large", "region": "local", "vram": "24"},
            capacity={"vram_mb": 24576, "ram_mb": 32768, "cpu": 1600},
            available={"vram_mb": 20000, "ram_mb": 28000, "cpu": 1200},
        )
    )
    sched.register_node(
        NodeProfile(
            node_id="ol1",
            labels={"gpu": "amd", "tier": "local", "region": "local", "vram": "10"},
            capacity={"vram_mb": 10240, "ram_mb": 48128, "cpu": 1600},
            available={"vram_mb": 8000, "ram_mb": 44000, "cpu": 1400},
            taints=["no-cuda"],
        )
    )

    return sched


async def main():
    import sys

    sched = build_jarvis_node_affinity()
    await sched.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        tasks = [
            TaskAffinitySpec(
                task_id="inference-001",
                required_rules=[
                    AffinityRule(
                        "r1",
                        AffinityType.REQUIRED,
                        [LabelSelector("gpu", MatchOperator.IN, ["nvidia"])],
                    )
                ],
                resource_request={"vram_mb": 4096},
            ),
            TaskAffinitySpec(
                task_id="reasoning-001",
                preferred_rules=[
                    AffinityRule(
                        "p1",
                        AffinityType.PREFERRED,
                        [LabelSelector("vram", MatchOperator.GT, numeric_value=20)],
                        weight=10,
                    )
                ],
                resource_request={"vram_mb": 8000},
            ),
            TaskAffinitySpec(
                task_id="local-001",
                required_rules=[
                    AffinityRule(
                        "r2",
                        AffinityType.REQUIRED,
                        [LabelSelector("tier", MatchOperator.EQ, ["local"])],
                    )
                ],
                tolerate_taints=["no-cuda"],
                resource_request={"ram_mb": 1024},
            ),
        ]

        print("Placing tasks:")
        for spec in tasks:
            result = sched.place(spec)
            icon = "✅" if result.placed else "❌"
            print(
                f"  {icon} {spec.task_id:<20} → node={result.node_id or 'NONE':<5} "
                f"score={result.score:.1f} ({result.reason})"
            )
            for nid, sc in sorted(result.scores_by_node.items(), key=lambda x: -x[1]):
                print(f"       {nid}: {sc:.1f}")

        print(f"\nStats: {json.dumps(sched.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(sched.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
