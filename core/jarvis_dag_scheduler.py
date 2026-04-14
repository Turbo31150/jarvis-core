#!/usr/bin/env python3
"""
jarvis_dag_scheduler — DAG-based task scheduling with dependency resolution
Execute tasks in dependency order, parallel when possible, track completion
"""

import asyncio
import json
import logging
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.dag_scheduler")

REDIS_KEY = "jarvis:dag"


@dataclass
class DAGNode:
    node_id: str
    name: str
    coro_factory: Callable
    deps: list[str] = field(default_factory=list)  # node_ids that must complete first
    status: str = "pending"  # pending | running | ok | error | skipped
    result: Any = None
    error: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0
    retries: int = 0
    max_retries: int = 1

    @property
    def duration_s(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.ended_at or time.time()
        return round(end - self.started_at, 3)

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "name": self.name,
            "deps": self.deps,
            "status": self.status,
            "duration_s": self.duration_s,
            "error": self.error[:100] if self.error else "",
        }


@dataclass
class DAGRun:
    run_id: str
    dag_name: str
    nodes: dict[str, DAGNode] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    ended_at: float = 0.0
    status: str = "running"

    @property
    def duration_s(self) -> float:
        end = self.ended_at or time.time()
        return round(end - self.started_at, 3)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "dag_name": self.dag_name,
            "status": self.status,
            "duration_s": self.duration_s,
            "nodes": {nid: n.to_dict() for nid, n in self.nodes.items()},
        }


class DAGScheduler:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._runs: dict[str, DAGRun] = {}

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def _topological_levels(self, nodes: dict[str, DAGNode]) -> list[list[str]]:
        """Group nodes into levels that can run in parallel."""
        in_degree: dict[str, int] = {nid: 0 for nid in nodes}
        children: dict[str, list[str]] = defaultdict(list)

        for nid, node in nodes.items():
            for dep in node.deps:
                if dep in nodes:
                    in_degree[nid] += 1
                    children[dep].append(nid)

        levels = []
        ready = [nid for nid, deg in in_degree.items() if deg == 0]

        while ready:
            levels.append(ready)
            next_ready = []
            for nid in ready:
                for child in children[nid]:
                    in_degree[child] -= 1
                    if in_degree[child] == 0:
                        next_ready.append(child)
            ready = next_ready

        return levels

    def _check_deps_ok(self, node: DAGNode, all_nodes: dict[str, DAGNode]) -> bool:
        """Return True if all dependencies completed successfully."""
        for dep_id in node.deps:
            dep = all_nodes.get(dep_id)
            if not dep or dep.status != "ok":
                return False
        return True

    async def _execute_node(self, node: DAGNode, all_nodes: dict[str, DAGNode]) -> None:
        if not self._check_deps_ok(node, all_nodes):
            node.status = "skipped"
            node.error = "dependency failed"
            return

        for attempt in range(node.max_retries + 1):
            node.status = "running"
            node.started_at = time.time()
            try:
                # Pass dependency results as context if factory accepts them
                dep_results = {
                    dep_id: all_nodes[dep_id].result
                    for dep_id in node.deps
                    if dep_id in all_nodes
                }
                result = await node.coro_factory(dep_results)
                node.result = result
                node.status = "ok"
                node.ended_at = time.time()
                log.debug(f"Node {node.name} OK ({node.duration_s:.2f}s)")
                return
            except Exception as e:
                node.error = str(e)
                node.retries = attempt
                if attempt < node.max_retries:
                    log.warning(
                        f"Node {node.name} retry {attempt + 1}/{node.max_retries}: {e}"
                    )
                    await asyncio.sleep(2**attempt)
                else:
                    node.status = "error"
                    node.ended_at = time.time()
                    log.error(
                        f"Node {node.name} FAILED after {attempt + 1} attempts: {e}"
                    )

    async def run(
        self,
        dag_name: str,
        nodes: list[dict],
        fail_fast: bool = False,
    ) -> DAGRun:
        run_id = str(uuid.uuid4())[:8]
        dag_nodes: dict[str, DAGNode] = {}

        for nd in nodes:
            node = DAGNode(
                node_id=nd["id"],
                name=nd.get("name", nd["id"]),
                coro_factory=nd["fn"],
                deps=nd.get("deps", []),
                max_retries=nd.get("retries", 0),
            )
            dag_nodes[node.node_id] = node

        run = DAGRun(run_id=run_id, dag_name=dag_name, nodes=dag_nodes)
        self._runs[run_id] = run

        levels = self._topological_levels(dag_nodes)
        log.info(
            f"DAG {dag_name} [{run_id}]: {len(dag_nodes)} nodes, {len(levels)} levels"
        )

        for level_idx, level_nodes in enumerate(levels):
            tasks = [
                self._execute_node(dag_nodes[nid], dag_nodes) for nid in level_nodes
            ]
            await asyncio.gather(*tasks)

            if fail_fast:
                failed = [
                    nid for nid in level_nodes if dag_nodes[nid].status == "error"
                ]
                if failed:
                    log.error(f"Fail-fast: {failed}")
                    break

        run.ended_at = time.time()
        ok = sum(1 for n in dag_nodes.values() if n.status == "ok")
        total = len(dag_nodes)
        run.status = "ok" if ok == total else ("partial" if ok > 0 else "error")

        log.info(
            f"DAG {dag_name} [{run_id}] {run.status}: {ok}/{total} in {run.duration_s:.2f}s"
        )

        if self.redis:
            await self.redis.set(
                f"{REDIS_KEY}:{run_id}",
                json.dumps(run.to_dict()),
                ex=3600,
            )
            await self.redis.publish(
                "jarvis:events",
                json.dumps(
                    {
                        "event": "dag_complete",
                        "run_id": run_id,
                        "dag": dag_name,
                        "status": run.status,
                        "duration_s": run.duration_s,
                        "ok": ok,
                        "total": total,
                    }
                ),
            )

        return run

    def get_run(self, run_id: str) -> DAGRun | None:
        return self._runs.get(run_id)


async def main():
    import sys

    sched = DAGScheduler()
    await sched.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":

        async def fetch(_):
            await asyncio.sleep(0.1)
            return {"data": [1, 2, 3]}

        async def process(deps):
            data = deps.get("fetch", {}).get("data", [])
            await asyncio.sleep(0.05)
            return {"sum": sum(data)}

        async def store(deps):
            result = deps.get("process", {}).get("sum", 0)
            await asyncio.sleep(0.05)
            return {"stored": True, "value": result}

        async def notify(_):
            return {"notified": True}

        nodes = [
            {"id": "fetch", "name": "Fetch data", "fn": fetch, "deps": []},
            {"id": "process", "name": "Process", "fn": process, "deps": ["fetch"]},
            {"id": "store", "name": "Store", "fn": store, "deps": ["process"]},
            {"id": "notify", "name": "Notify", "fn": notify, "deps": ["store"]},
        ]

        run = await sched.run("demo_dag", nodes)
        print(f"\nDAG: {run.dag_name} [{run.run_id}]")
        print(f"Status: {run.status} | Duration: {run.duration_s:.2f}s\n")
        for nid, node in run.nodes.items():
            icon = "✅" if node.status == "ok" else "❌"
            print(f"  {icon} {node.name:<20} {node.duration_s:.3f}s → {node.result}")

    elif cmd == "get" and len(sys.argv) > 2:
        run = sched.get_run(sys.argv[2])
        if run:
            print(json.dumps(run.to_dict(), indent=2))
        else:
            print(f"Run '{sys.argv[2]}' not found")


if __name__ == "__main__":
    asyncio.run(main())
