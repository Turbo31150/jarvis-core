#!/usr/bin/env python3
"""
jarvis_dependency_resolver — Service and module dependency graph resolver
Topological sort, cycle detection, and parallel execution tier ordering
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.dependency_resolver")

REDIS_PREFIX = "jarvis:deps:"


class DepState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class DepNode:
    node_id: str
    label: str = ""
    deps: list[str] = field(default_factory=list)  # node_ids this depends on
    optional_deps: list[str] = field(default_factory=list)
    state: DepState = DepState.PENDING
    metadata: dict = field(default_factory=dict)
    started_at: float = 0.0
    finished_at: float = 0.0
    error: str = ""

    @property
    def duration_ms(self) -> float:
        if self.started_at and self.finished_at:
            return (self.finished_at - self.started_at) * 1000
        return 0.0

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "label": self.label,
            "deps": self.deps,
            "optional_deps": self.optional_deps,
            "state": self.state.value,
            "duration_ms": round(self.duration_ms, 2),
            "error": self.error,
        }


class CycleError(Exception):
    pass


class DependencyResolver:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._nodes: dict[str, DepNode] = {}
        self._stats: dict[str, int] = {
            "resolves": 0,
            "cycles_detected": 0,
            "nodes_run": 0,
            "nodes_failed": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def add(
        self,
        node_id: str,
        deps: list[str] | None = None,
        optional_deps: list[str] | None = None,
        label: str = "",
        metadata: dict | None = None,
    ) -> DepNode:
        node = DepNode(
            node_id=node_id,
            label=label or node_id,
            deps=deps or [],
            optional_deps=optional_deps or [],
            metadata=metadata or {},
        )
        self._nodes[node_id] = node
        return node

    def _all_deps(self, node: DepNode) -> list[str]:
        """Required + optional (only those that exist)."""
        return node.deps + [d for d in node.optional_deps if d in self._nodes]

    def topo_sort(self, node_ids: list[str] | None = None) -> list[str]:
        """Returns nodes in topological order. Raises CycleError on cycles."""
        ids = node_ids or list(self._nodes.keys())
        visited: set[str] = set()
        in_stack: set[str] = set()
        result: list[str] = []

        def dfs(nid: str):
            if nid in in_stack:
                raise CycleError(f"Cycle detected at node: {nid}")
            if nid in visited:
                return
            in_stack.add(nid)
            node = self._nodes.get(nid)
            if node:
                for dep in self._all_deps(node):
                    if dep in self._nodes:
                        dfs(dep)
            in_stack.discard(nid)
            visited.add(nid)
            result.append(nid)

        for nid in ids:
            dfs(nid)
        return result

    def execution_tiers(self, node_ids: list[str] | None = None) -> list[list[str]]:
        """
        Group nodes into parallel execution tiers.
        All nodes in a tier can run concurrently (their deps are in earlier tiers).
        """
        ids = set(node_ids or self._nodes.keys())
        completed: set[str] = set()
        tiers: list[list[str]] = []

        remaining = set(ids)
        while remaining:
            # Find nodes whose required deps are all complete
            tier = []
            for nid in sorted(remaining):
                node = self._nodes.get(nid)
                if not node:
                    tier.append(nid)
                    continue
                required = [d for d in node.deps if d in ids]
                if all(d in completed for d in required):
                    tier.append(nid)

            if not tier:
                raise CycleError(f"Cannot resolve remaining: {remaining}")

            tiers.append(tier)
            completed.update(tier)
            remaining -= set(tier)

        return tiers

    def detect_cycles(self) -> list[list[str]]:
        """Returns list of cycles found (each cycle as list of node_ids)."""
        cycles: list[list[str]] = []
        visited: set[str] = set()
        in_stack: list[str] = []

        def dfs(nid: str):
            if nid in in_stack:
                idx = in_stack.index(nid)
                cycles.append(in_stack[idx:] + [nid])
                return
            if nid in visited:
                return
            visited.add(nid)
            in_stack.append(nid)
            node = self._nodes.get(nid)
            if node:
                for dep in node.deps:
                    if dep in self._nodes:
                        dfs(dep)
            in_stack.pop()

        for nid in self._nodes:
            dfs(nid)
        return cycles

    async def execute(
        self,
        runner: Any,  # async callable: runner(node_id, metadata) → Any
        node_ids: list[str] | None = None,
        fail_fast: bool = True,
    ) -> dict[str, Any]:
        """
        Execute nodes tier by tier, calling runner for each.
        Returns {node_id: result_or_exception}.
        """
        self._stats["resolves"] += 1
        results: dict[str, Any] = {}

        try:
            tiers = self.execution_tiers(node_ids)
        except CycleError:
            self._stats["cycles_detected"] += 1
            raise

        failed: set[str] = set()

        for tier in tiers:
            # Skip nodes whose required deps failed
            runnable = []
            for nid in tier:
                node = self._nodes.get(nid)
                if not node:
                    continue
                required_failed = [d for d in node.deps if d in failed]
                if required_failed:
                    node.state = DepState.SKIPPED
                    log.warning(f"Skipping {nid}: deps failed {required_failed}")
                    continue
                runnable.append(nid)

            if not runnable:
                continue

            async def run_one(nid: str) -> tuple[str, Any]:
                node = self._nodes[nid]
                node.state = DepState.RUNNING
                node.started_at = time.time()
                try:
                    result = await runner(nid, node.metadata)
                    node.state = DepState.DONE
                    node.finished_at = time.time()
                    self._stats["nodes_run"] += 1
                    return nid, result
                except Exception as e:
                    node.state = DepState.FAILED
                    node.finished_at = time.time()
                    node.error = str(e)[:200]
                    self._stats["nodes_failed"] += 1
                    return nid, e

            tier_results = await asyncio.gather(*[run_one(nid) for nid in runnable])

            for nid, result in tier_results:
                results[nid] = result
                if isinstance(result, Exception):
                    failed.add(nid)
                    if fail_fast:
                        raise result

        return results

    def reset(self):
        for node in self._nodes.values():
            node.state = DepState.PENDING
            node.started_at = 0.0
            node.finished_at = 0.0
            node.error = ""

    def graph(self) -> dict:
        return {
            "nodes": [n.to_dict() for n in self._nodes.values()],
            "edges": [
                {"from": dep, "to": nid}
                for nid, node in self._nodes.items()
                for dep in node.deps
            ],
        }

    def stats(self) -> dict:
        return {
            **self._stats,
            "total_nodes": len(self._nodes),
        }


def build_jarvis_dependency_resolver() -> DependencyResolver:
    r = DependencyResolver()

    # Example: JARVIS boot dependency graph
    r.add("redis", label="Redis server")
    r.add("config-loader", deps=["redis"], label="Load config from Redis")
    r.add("gpu-monitor", deps=["config-loader"], label="GPU thermal watcher")
    r.add(
        "model-warmer", deps=["config-loader", "gpu-monitor"], label="Warm LLM models"
    )
    r.add("inference-gw", deps=["model-warmer", "redis"], label="Inference gateway")
    r.add("trading-agent", deps=["inference-gw"], label="Trading agent")
    r.add("monitoring", deps=["redis", "gpu-monitor"], label="System monitoring")

    return r


async def main():
    import sys

    resolver = build_jarvis_dependency_resolver()
    await resolver.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "tiers"

    if cmd == "tiers":
        tiers = resolver.execution_tiers()
        print("Execution tiers (parallel within each tier):")
        for i, tier in enumerate(tiers):
            print(f"  Tier {i + 1}: {tier}")

    elif cmd == "graph":
        print(json.dumps(resolver.graph(), indent=2))

    elif cmd == "cycles":
        cycles = resolver.detect_cycles()
        if cycles:
            print(f"Cycles detected: {cycles}")
        else:
            print("No cycles detected.")

    elif cmd == "exec":

        async def mock_runner(nid: str, meta: dict) -> str:
            await asyncio.sleep(0.05)
            print(f"  Running: {nid}")
            return f"done:{nid}"

        results = await resolver.execute(mock_runner)
        print(f"Results: {list(results.keys())}")
        print(f"Stats: {json.dumps(resolver.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(resolver.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
