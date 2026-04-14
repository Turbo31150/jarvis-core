#!/usr/bin/env python3
"""
jarvis_model_router_v3 — Smart model routing with cost/quality/speed tradeoffs
Routes requests to optimal model based on task type, load, budget, and SLA
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.model_router_v3")

REDIS_PREFIX = "jarvis:router3:"

NODES = {
    "M1": "http://127.0.0.1:1234",
    "M2": "http://192.168.1.26:1234",
    "OL1": "http://127.0.0.1:11434",
}


class RoutingPolicy(str, Enum):
    QUALITY = "quality"  # Best model regardless of cost
    SPEED = "speed"  # Fastest response
    COST = "cost"  # Cheapest (least tokens, smallest model)
    BALANCED = "balanced"  # Quality/speed tradeoff
    FAILOVER = "failover"  # Try in order until one succeeds


class TaskType(str, Enum):
    CHAT = "chat"
    CODE = "code"
    REASONING = "reasoning"
    EMBEDDING = "embedding"
    CREATIVE = "creative"
    CLASSIFICATION = "classification"
    EXTRACTION = "extraction"
    SUMMARY = "summary"
    FAST = "fast"


@dataclass
class ModelSpec:
    model_id: str
    node: str
    task_types: list[TaskType]
    quality_score: float  # 0-1
    speed_score: float  # 0-1 (higher = faster)
    cost_score: float  # 0-1 (higher = cheaper)
    context_limit: int = 8192
    available: bool = True
    avg_latency_ms: float = 0.0
    error_rate: float = 0.0

    @property
    def composite_score(self) -> float:
        return (self.quality_score + self.speed_score + self.cost_score) / 3

    def to_dict(self) -> dict:
        return {
            "model_id": self.model_id,
            "node": self.node,
            "quality": self.quality_score,
            "speed": self.speed_score,
            "cost": self.cost_score,
            "available": self.available,
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "error_rate": round(self.error_rate, 3),
        }


MODEL_REGISTRY: list[ModelSpec] = [
    ModelSpec(
        "qwen/qwen3.5-9b",
        "M1",
        [TaskType.CHAT, TaskType.CODE, TaskType.SUMMARY, TaskType.EXTRACTION],
        quality_score=0.82,
        speed_score=0.90,
        cost_score=0.95,
        context_limit=32768,
    ),
    ModelSpec(
        "qwen/qwen3.5-27b-claude",
        "M1",
        [TaskType.CHAT, TaskType.CODE, TaskType.REASONING, TaskType.CREATIVE],
        quality_score=0.94,
        speed_score=0.65,
        cost_score=0.70,
        context_limit=32768,
    ),
    ModelSpec(
        "qwen/qwen3.5-35b-a3b",
        "M2",
        [TaskType.CHAT, TaskType.CODE, TaskType.REASONING, TaskType.CREATIVE],
        quality_score=0.96,
        speed_score=0.50,
        cost_score=0.60,
        context_limit=32768,
    ),
    ModelSpec(
        "deepseek-r1-0528",
        "M2",
        [TaskType.REASONING, TaskType.CODE],
        quality_score=0.98,
        speed_score=0.30,
        cost_score=0.50,
        context_limit=65536,
    ),
    ModelSpec(
        "glm-4.7-flash-claude",
        "M1",
        [TaskType.CHAT, TaskType.FAST, TaskType.CLASSIFICATION],
        quality_score=0.75,
        speed_score=0.98,
        cost_score=0.99,
        context_limit=8192,
    ),
    ModelSpec(
        "nomic-embed-text",
        "M2",
        [TaskType.EMBEDDING],
        quality_score=0.90,
        speed_score=0.95,
        cost_score=0.99,
        context_limit=8192,
    ),
    ModelSpec(
        "qwen/qwen3.5-9b",
        "M2",
        [TaskType.CHAT, TaskType.CODE, TaskType.SUMMARY],
        quality_score=0.82,
        speed_score=0.85,
        cost_score=0.95,
        context_limit=32768,
    ),
]


@dataclass
class RoutingDecision:
    model_id: str
    node: str
    node_url: str
    policy: RoutingPolicy
    task_type: TaskType
    score: float
    reason: str
    fallbacks: list[str] = field(default_factory=list)
    latency_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "model_id": self.model_id,
            "node": self.node,
            "node_url": self.node_url,
            "policy": self.policy.value,
            "task_type": self.task_type.value,
            "score": round(self.score, 3),
            "reason": self.reason,
            "fallbacks": self.fallbacks,
        }


class ModelRouterV3:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._registry = list(MODEL_REGISTRY)
        self._node_health: dict[str, bool] = {n: True for n in NODES}
        self._stats: dict[str, int] = {}

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def _candidates(
        self, task_type: TaskType, exclude_nodes: set[str] | None = None
    ) -> list[ModelSpec]:
        exclude = exclude_nodes or set()
        return [
            m
            for m in self._registry
            if task_type in m.task_types
            and m.available
            and self._node_health.get(m.node, True)
            and m.node not in exclude
        ]

    def _score(self, model: ModelSpec, policy: RoutingPolicy) -> float:
        weights = {
            RoutingPolicy.QUALITY: (0.7, 0.2, 0.1),
            RoutingPolicy.SPEED: (0.1, 0.7, 0.2),
            RoutingPolicy.COST: (0.1, 0.2, 0.7),
            RoutingPolicy.BALANCED: (0.4, 0.3, 0.3),
            RoutingPolicy.FAILOVER: (0.33, 0.33, 0.34),
        }
        wq, ws, wc = weights.get(policy, (0.33, 0.33, 0.34))
        # Penalize high error rate
        penalty = model.error_rate * 0.5
        return (
            model.quality_score * wq + model.speed_score * ws + model.cost_score * wc
        ) - penalty

    async def route(
        self,
        task_type: TaskType = TaskType.CHAT,
        policy: RoutingPolicy = RoutingPolicy.BALANCED,
        required_context: int = 0,
        exclude_nodes: set[str] | None = None,
    ) -> RoutingDecision:
        t0 = time.time()
        candidates = self._candidates(task_type, exclude_nodes)

        if required_context:
            candidates = [c for c in candidates if c.context_limit >= required_context]

        if not candidates:
            # Fallback: any available model
            candidates = [
                m
                for m in self._registry
                if m.available and self._node_health.get(m.node, True)
            ]

        if not candidates:
            raise RuntimeError(f"No models available for task={task_type.value}")

        scored = sorted(candidates, key=lambda m: self._score(m, policy), reverse=True)
        best = scored[0]
        fallbacks = [f"{m.node}:{m.model_id}" for m in scored[1:3]]

        decision = RoutingDecision(
            model_id=best.model_id,
            node=best.node,
            node_url=NODES[best.node],
            policy=policy,
            task_type=task_type,
            score=self._score(best, policy),
            reason=f"Best {policy.value} score among {len(candidates)} candidates",
            fallbacks=fallbacks,
            latency_ms=round((time.time() - t0) * 1000, 1),
        )

        key = f"{task_type.value}:{best.model_id}"
        self._stats[key] = self._stats.get(key, 0) + 1

        if self.redis:
            await self.redis.hincrby(f"{REDIS_PREFIX}stats", key, 1)

        log.debug(
            f"Route: {task_type.value}/{policy.value} → {best.node}:{best.model_id} score={decision.score:.3f}"
        )
        return decision

    def update_health(self, node: str, alive: bool):
        self._node_health[node] = alive
        for m in self._registry:
            if m.node == node:
                m.available = alive

    def record_outcome(
        self, model_id: str, node: str, latency_ms: float, success: bool
    ):
        for m in self._registry:
            if m.model_id == model_id and m.node == node:
                alpha = 0.15
                m.avg_latency_ms = alpha * latency_ms + (1 - alpha) * (
                    m.avg_latency_ms or latency_ms
                )
                if not success:
                    m.error_rate = alpha * 1.0 + (1 - alpha) * m.error_rate
                else:
                    m.error_rate = alpha * 0.0 + (1 - alpha) * m.error_rate

    async def probe_nodes(self):
        for node, url in NODES.items():
            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=2)
                ) as sess:
                    async with sess.get(f"{url}/v1/models") as r:
                        self.update_health(node, r.status == 200)
            except Exception:
                self.update_health(node, False)

    def registry_status(self) -> list[dict]:
        return [m.to_dict() for m in self._registry]

    def stats(self) -> dict:
        return {
            "node_health": self._node_health,
            "routes": dict(self._stats),
            "total_routes": sum(self._stats.values()),
            "registry_size": len(self._registry),
        }


async def main():
    import sys

    router = ModelRouterV3()
    await router.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        scenarios = [
            (TaskType.CHAT, RoutingPolicy.BALANCED),
            (TaskType.CODE, RoutingPolicy.QUALITY),
            (TaskType.REASONING, RoutingPolicy.QUALITY),
            (TaskType.FAST, RoutingPolicy.SPEED),
            (TaskType.EMBEDDING, RoutingPolicy.COST),
        ]
        print(f"{'Task':<15} {'Policy':<12} {'Node':<5} {'Model':<35} {'Score':>6}")
        print("-" * 80)
        for task, policy in scenarios:
            try:
                d = await router.route(task, policy)
                print(
                    f"  {task.value:<15} {policy.value:<12} {d.node:<5} {d.model_id:<35} {d.score:>5.3f}"
                )
            except Exception as e:
                print(f"  {task.value:<15} ERROR: {e}")

    elif cmd == "probe":
        await router.probe_nodes()
        for node, alive in router._node_health.items():
            print(f"  {'✅' if alive else '❌'} {node}")

    elif cmd == "stats":
        print(json.dumps(router.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
