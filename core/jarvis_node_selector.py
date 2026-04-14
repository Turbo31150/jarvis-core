#!/usr/bin/env python3
"""
jarvis_node_selector — Smart node selection for LLM task dispatch
Selects optimal cluster node based on model availability, load, latency, and cost
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.node_selector")

REDIS_PREFIX = "jarvis:nodesel:"


class SelectionStrategy(str, Enum):
    LOWEST_LATENCY = "lowest_latency"
    LEAST_LOADED = "least_loaded"
    ROUND_ROBIN = "round_robin"
    WEIGHTED = "weighted"
    FASTEST_MODEL = "fastest_model"


@dataclass
class NodeProfile:
    name: str
    base_url: str
    models: list[str] = field(default_factory=list)
    weight: int = 10
    gpu_count: int = 1
    vram_gb: float = 8.0
    priority: int = 5  # higher = prefer this node

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "base_url": self.base_url,
            "models": self.models,
            "weight": self.weight,
            "gpu_count": self.gpu_count,
            "vram_gb": self.vram_gb,
            "priority": self.priority,
        }


@dataclass
class NodeMetrics:
    name: str
    available: bool = True
    latency_ms: float = 999.0
    active_requests: int = 0
    error_rate: float = 0.0
    last_probe: float = 0.0
    loaded_models: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "available": self.available,
            "latency_ms": round(self.latency_ms, 1),
            "active_requests": self.active_requests,
            "error_rate": round(self.error_rate * 100, 1),
            "last_probe": self.last_probe,
            "loaded_models": self.loaded_models,
        }


@dataclass
class SelectionResult:
    node_name: str
    base_url: str
    model: str
    strategy: SelectionStrategy
    score: float
    reason: str
    alternatives: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "node": self.node_name,
            "base_url": self.base_url,
            "model": self.model,
            "strategy": self.strategy.value,
            "score": round(self.score, 3),
            "reason": self.reason,
            "alternatives": self.alternatives,
        }


class NodeSelector:
    def __init__(self, strategy: SelectionStrategy = SelectionStrategy.WEIGHTED):
        self.redis: aioredis.Redis | None = None
        self.strategy = strategy
        self._nodes: dict[str, NodeProfile] = {}
        self._metrics: dict[str, NodeMetrics] = {}
        self._rr_index: int = 0
        self._history: list[dict] = []
        self._probe_cache_ttl_s = 15.0

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def register(self, profile: NodeProfile):
        self._nodes[profile.name] = profile
        self._metrics[profile.name] = NodeMetrics(name=profile.name)

    async def probe_node(self, name: str) -> NodeMetrics:
        profile = self._nodes[name]
        metrics = self._metrics[name]

        # Skip if recently probed
        if time.time() - metrics.last_probe < self._probe_cache_ttl_s:
            return metrics

        t0 = time.time()
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=3)
            ) as sess:
                async with sess.get(f"{profile.base_url}/v1/models") as r:
                    latency = (time.time() - t0) * 1000
                    if r.status == 200:
                        data = await r.json(content_type=None)
                        models = [m.get("id", "") for m in data.get("data", [])]
                        metrics.loaded_models = models
                        metrics.available = True
                        metrics.latency_ms = latency
                    else:
                        metrics.available = False
        except Exception as e:
            metrics.available = False
            metrics.latency_ms = 9999.0
            log.debug(f"Probe failed for {name}: {e}")

        metrics.last_probe = time.time()
        return metrics

    async def probe_all(self):
        await asyncio.gather(*[self.probe_node(n) for n in self._nodes])

    def _has_model(self, name: str, model: str) -> bool:
        profile = self._nodes[name]
        metrics = self._metrics[name]
        candidates = metrics.loaded_models or profile.models
        return any(
            model.lower() in m.lower() or m.lower() in model.lower() for m in candidates
        )

    def _score(self, name: str, model: str) -> float:
        profile = self._nodes[name]
        metrics = self._metrics[name]
        if not metrics.available:
            return -1.0

        model_bonus = 0.3 if self._has_model(name, model) else 0.0

        if self.strategy == SelectionStrategy.LOWEST_LATENCY:
            lat_score = 1.0 - min(metrics.latency_ms / 2000.0, 1.0)
            return lat_score + model_bonus

        if self.strategy == SelectionStrategy.LEAST_LOADED:
            load_score = 1.0 - min(metrics.active_requests / 20.0, 1.0)
            return load_score + model_bonus

        if self.strategy == SelectionStrategy.WEIGHTED:
            lat_score = 1.0 - min(metrics.latency_ms / 2000.0, 1.0)
            load_score = 1.0 - min(metrics.active_requests / 20.0, 1.0)
            err_penalty = metrics.error_rate
            weight_norm = profile.weight / 10.0
            pri_norm = profile.priority / 10.0
            return (
                0.3 * lat_score
                + 0.2 * load_score
                + 0.2 * model_bonus
                + 0.2 * weight_norm
                + 0.1 * pri_norm
                - 0.3 * err_penalty
            )

        if self.strategy == SelectionStrategy.FASTEST_MODEL:
            return model_bonus * 2.0 + (1.0 - min(metrics.latency_ms / 2000.0, 1.0))

        return float(profile.weight)

    def select(self, model: str) -> SelectionResult | None:
        available = [n for n, m in self._metrics.items() if m.available]
        if not available:
            return None

        if self.strategy == SelectionStrategy.ROUND_ROBIN:
            self._rr_index = (self._rr_index + 1) % len(available)
            chosen = available[self._rr_index % len(available)]
            profile = self._nodes[chosen]
            return SelectionResult(
                node_name=chosen,
                base_url=profile.base_url,
                model=model,
                strategy=self.strategy,
                score=1.0,
                reason="round_robin",
                alternatives=[n for n in available if n != chosen],
            )

        scores = [(n, self._score(n, model)) for n in available]
        scores.sort(key=lambda x: -x[1])
        best_name, best_score = scores[0]
        profile = self._nodes[best_name]

        reason = f"strategy={self.strategy.value}"
        if self._has_model(best_name, model):
            reason += ", model_loaded"

        result = SelectionResult(
            node_name=best_name,
            base_url=profile.base_url,
            model=model,
            strategy=self.strategy,
            score=best_score,
            reason=reason,
            alternatives=[n for n, _ in scores[1:3]],
        )
        self._history.append(result.to_dict())
        return result

    def node_status(self) -> list[dict]:
        return [
            {**self._nodes[n].to_dict(), **self._metrics[n].to_dict()}
            for n in self._nodes
        ]

    def stats(self) -> dict:
        available = sum(1 for m in self._metrics.values() if m.available)
        return {
            "nodes": len(self._nodes),
            "available": available,
            "strategy": self.strategy.value,
            "selections": len(self._history),
        }


def build_jarvis_selector() -> NodeSelector:
    sel = NodeSelector(strategy=SelectionStrategy.WEIGHTED)
    sel.register(
        NodeProfile(
            "m1",
            "http://192.168.1.85:1234",
            models=["qwen3.5-9b", "qwen3.5-27b-claude", "glm-4.7-flash-claude"],
            weight=10,
            gpu_count=3,
            vram_gb=36.0,
            priority=9,
        )
    )
    sel.register(
        NodeProfile(
            "m2",
            "http://192.168.1.26:1234",
            models=["deepseek-r1-0528", "qwen3.5-35b-a3b", "nomic-embed-text"],
            weight=8,
            gpu_count=1,
            vram_gb=10.0,
            priority=7,
        )
    )
    sel.register(
        NodeProfile(
            "ol1",
            "http://127.0.0.1:11434",
            models=["gemma3:4b", "llama3.2"],
            weight=4,
            gpu_count=0,
            vram_gb=0.0,
            priority=3,
        )
    )
    return sel


async def main():
    import sys

    selector = build_jarvis_selector()
    await selector.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Probing nodes...")
        await selector.probe_all()
        for ns in selector.node_status():
            avail = "✅" if ns["available"] else "❌"
            print(
                f"  {avail} {ns['name']:<6} {ns['latency_ms']:>6.0f}ms  models={len(ns['loaded_models'])}"
            )

        models = ["qwen3.5-9b", "deepseek-r1-0528", "gemma3:4b", "unknown-model"]
        print("\nSelections:")
        for model in models:
            result = selector.select(model)
            if result:
                print(
                    f"  {model:<30} → {result.node_name} (score={result.score:.3f}) {result.reason}"
                )
            else:
                print(f"  {model:<30} → NO NODE AVAILABLE")

        print(f"\nStats: {json.dumps(selector.stats(), indent=2)}")

    elif cmd == "status":
        await selector.probe_all()
        for ns in selector.node_status():
            print(json.dumps(ns, indent=2))

    elif cmd == "stats":
        print(json.dumps(selector.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
