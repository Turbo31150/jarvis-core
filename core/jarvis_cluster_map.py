#!/usr/bin/env python3
"""
jarvis_cluster_map — Live topology map of the JARVIS cluster
Tracks nodes, edges, services, and GPU resources with real-time health overlay
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.cluster_map")

REDIS_PREFIX = "jarvis:clustermap:"


class NodeKind(str, Enum):
    LLM_SERVER = "llm_server"
    OLLAMA = "ollama"
    REDIS = "redis"
    AGENT = "agent"
    GATEWAY = "gateway"
    MONITOR = "monitor"
    STORAGE = "storage"


class EdgeKind(str, Enum):
    HTTP = "http"
    REDIS_PUBSUB = "redis_pubsub"
    REDIS_STREAM = "redis_stream"
    TCP = "tcp"
    INTERNAL = "internal"


class NodeHealth(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


@dataclass
class GPUInfo:
    gpu_id: int
    name: str
    vram_total_mb: int
    vram_used_mb: int = 0
    temperature_c: float = 0.0
    utilization_pct: float = 0.0

    @property
    def vram_free_mb(self) -> int:
        return max(0, self.vram_total_mb - self.vram_used_mb)

    @property
    def vram_pct(self) -> float:
        return self.vram_used_mb / max(self.vram_total_mb, 1) * 100

    def to_dict(self) -> dict:
        return {
            "gpu_id": self.gpu_id,
            "name": self.name,
            "vram_total_mb": self.vram_total_mb,
            "vram_used_mb": self.vram_used_mb,
            "vram_free_mb": self.vram_free_mb,
            "vram_pct": round(self.vram_pct, 1),
            "temperature_c": self.temperature_c,
            "utilization_pct": self.utilization_pct,
        }


@dataclass
class ClusterNode:
    node_id: str
    kind: NodeKind
    hostname: str
    ip: str
    port: int
    health: NodeHealth = NodeHealth.UNKNOWN
    labels: dict[str, str] = field(default_factory=dict)
    gpus: list[GPUInfo] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    cpu_pct: float = 0.0
    ram_used_mb: int = 0
    ram_total_mb: int = 0
    last_seen: float = 0.0
    latency_ms: float = 0.0
    metadata: dict = field(default_factory=dict)

    @property
    def endpoint_url(self) -> str:
        return f"http://{self.ip}:{self.port}"

    @property
    def stale(self) -> bool:
        return self.last_seen > 0 and (time.time() - self.last_seen) > 120.0

    @property
    def total_vram_mb(self) -> int:
        return sum(g.vram_total_mb for g in self.gpus)

    @property
    def free_vram_mb(self) -> int:
        return sum(g.vram_free_mb for g in self.gpus)

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "kind": self.kind.value,
            "hostname": self.hostname,
            "ip": self.ip,
            "port": self.port,
            "health": self.health.value,
            "labels": self.labels,
            "models": self.models,
            "cpu_pct": round(self.cpu_pct, 1),
            "ram_used_mb": self.ram_used_mb,
            "ram_total_mb": self.ram_total_mb,
            "gpus": [g.to_dict() for g in self.gpus],
            "total_vram_mb": self.total_vram_mb,
            "free_vram_mb": self.free_vram_mb,
            "latency_ms": round(self.latency_ms, 1),
            "last_seen": self.last_seen,
            "stale": self.stale,
        }


@dataclass
class ClusterEdge:
    from_node: str
    to_node: str
    kind: EdgeKind
    label: str = ""
    latency_ms: float = 0.0
    healthy: bool = True
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "from": self.from_node,
            "to": self.to_node,
            "kind": self.kind.value,
            "label": self.label,
            "latency_ms": round(self.latency_ms, 1),
            "healthy": self.healthy,
        }


async def _probe_lmstudio(node: ClusterNode, timeout_s: float = 5.0):
    t0 = time.time()
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout_s)
        ) as sess:
            async with sess.get(f"{node.endpoint_url}/v1/models") as r:
                node.latency_ms = (time.time() - t0) * 1000
                if r.status == 200:
                    data = await r.json(content_type=None)
                    node.models = [m.get("id", "") for m in data.get("data", [])]
                    node.health = NodeHealth.HEALTHY
                    node.last_seen = time.time()
                else:
                    node.health = NodeHealth.DEGRADED
    except Exception:
        node.health = NodeHealth.OFFLINE
        node.latency_ms = (time.time() - t0) * 1000


async def _probe_ollama(node: ClusterNode, timeout_s: float = 5.0):
    t0 = time.time()
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout_s)
        ) as sess:
            async with sess.get(f"{node.endpoint_url}/api/tags") as r:
                node.latency_ms = (time.time() - t0) * 1000
                if r.status == 200:
                    data = await r.json(content_type=None)
                    node.models = [m.get("name", "") for m in data.get("models", [])]
                    node.health = NodeHealth.HEALTHY
                    node.last_seen = time.time()
                else:
                    node.health = NodeHealth.DEGRADED
    except Exception:
        node.health = NodeHealth.OFFLINE
        node.latency_ms = (time.time() - t0) * 1000


async def _probe_redis(node: ClusterNode, timeout_s: float = 3.0):
    import redis.asyncio as aioredis

    t0 = time.time()
    try:
        r = aioredis.Redis(host=node.ip, port=node.port)
        await asyncio.wait_for(r.ping(), timeout=timeout_s)
        await r.aclose()
        node.latency_ms = (time.time() - t0) * 1000
        node.health = NodeHealth.HEALTHY
        node.last_seen = time.time()
    except Exception:
        node.health = NodeHealth.OFFLINE
        node.latency_ms = (time.time() - t0) * 1000


class ClusterMap:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._nodes: dict[str, ClusterNode] = {}
        self._edges: list[ClusterEdge] = []
        self._stats: dict[str, Any] = {
            "probes": 0,
            "healthy": 0,
            "degraded": 0,
            "offline": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def add_node(self, node: ClusterNode):
        self._nodes[node.node_id] = node

    def add_edge(self, edge: ClusterEdge):
        self._edges.append(edge)

    async def probe_node(self, node_id: str):
        node = self._nodes.get(node_id)
        if not node:
            return
        self._stats["probes"] += 1
        if node.kind in (NodeKind.LLM_SERVER,):
            await _probe_lmstudio(node)
        elif node.kind == NodeKind.OLLAMA:
            await _probe_ollama(node)
        elif node.kind == NodeKind.REDIS:
            await _probe_redis(node)

    async def probe_all(self, concurrency: int = 4):
        sem = asyncio.Semaphore(concurrency)

        async def bounded(nid: str):
            async with sem:
                await self.probe_node(nid)

        await asyncio.gather(*[bounded(nid) for nid in self._nodes])

        # Update stats
        self._stats["healthy"] = sum(
            1 for n in self._nodes.values() if n.health == NodeHealth.HEALTHY
        )
        self._stats["degraded"] = sum(
            1 for n in self._nodes.values() if n.health == NodeHealth.DEGRADED
        )
        self._stats["offline"] = sum(
            1 for n in self._nodes.values() if n.health == NodeHealth.OFFLINE
        )

        if self.redis:
            asyncio.create_task(self._redis_snapshot())

    async def _redis_snapshot(self):
        if not self.redis:
            return
        try:
            snapshot = json.dumps(self.topology())
            await self.redis.setex(f"{REDIS_PREFIX}snapshot", 300, snapshot)
        except Exception:
            pass

    def topology(self) -> dict:
        return {
            "nodes": [n.to_dict() for n in self._nodes.values()],
            "edges": [e.to_dict() for e in self._edges],
            "ts": time.time(),
        }

    def healthy_nodes(self, kind: NodeKind | None = None) -> list[ClusterNode]:
        nodes = [n for n in self._nodes.values() if n.health == NodeHealth.HEALTHY]
        if kind:
            nodes = [n for n in nodes if n.kind == kind]
        return nodes

    def find_model(self, model_pattern: str) -> list[ClusterNode]:
        return [
            n
            for n in self._nodes.values()
            if n.health == NodeHealth.HEALTHY
            and any(model_pattern.lower() in m.lower() for m in n.models)
        ]

    def stats(self) -> dict:
        return {
            **self._stats,
            "total_nodes": len(self._nodes),
            "edges": len(self._edges),
        }


def build_jarvis_cluster_map() -> ClusterMap:
    cm = ClusterMap()

    cm.add_node(
        ClusterNode(
            node_id="m1",
            kind=NodeKind.LLM_SERVER,
            hostname="m1",
            ip="192.168.1.85",
            port=1234,
            labels={"tier": "fast", "gpu": "nvidia"},
            gpus=[
                GPUInfo(0, "RTX 3060 12GB", 12288),
                GPUInfo(1, "GTX 1660 6GB", 6144),
            ],
            ram_total_mb=32768,
        )
    )
    cm.add_node(
        ClusterNode(
            node_id="m2",
            kind=NodeKind.LLM_SERVER,
            hostname="m2",
            ip="192.168.1.26",
            port=1234,
            labels={"tier": "large", "gpu": "nvidia"},
            gpus=[GPUInfo(0, "RTX 3090 24GB", 24576)],
            ram_total_mb=32768,
        )
    )
    cm.add_node(
        ClusterNode(
            node_id="ol1",
            kind=NodeKind.OLLAMA,
            hostname="ol1",
            ip="127.0.0.1",
            port=11434,
            labels={"tier": "local"},
            gpus=[GPUInfo(0, "RX 6700 XT 10GB", 10240)],
            ram_total_mb=48128,
        )
    )
    cm.add_node(
        ClusterNode(
            node_id="redis",
            kind=NodeKind.REDIS,
            hostname="localhost",
            ip="127.0.0.1",
            port=6379,
        )
    )

    cm.add_edge(ClusterEdge("m1", "redis", EdgeKind.TCP, "state"))
    cm.add_edge(ClusterEdge("m2", "redis", EdgeKind.TCP, "state"))
    cm.add_edge(ClusterEdge("ol1", "redis", EdgeKind.TCP, "state"))
    cm.add_edge(ClusterEdge("m1", "m2", EdgeKind.HTTP, "failover"))

    return cm


async def main():
    import sys

    cm = build_jarvis_cluster_map()
    await cm.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "probe"

    if cmd == "probe":
        print("Probing cluster nodes...")
        await cm.probe_all()

        print("\nCluster topology:")
        for node in cm._nodes.values():
            h = {
                "healthy": "✅",
                "degraded": "⚠️",
                "offline": "❌",
                "unknown": "❓",
            }.get(node.health.value, "?")
            print(
                f"  {h} {node.node_id:<6} [{node.kind.value:<12}] "
                f"{node.ip}:{node.port:<6} "
                f"models={len(node.models)} "
                f"latency={node.latency_ms:.0f}ms"
            )
            for gpu in node.gpus:
                print(f"       GPU{gpu.gpu_id}: {gpu.name} {gpu.vram_total_mb}MB")

        print(f"\nStats: {json.dumps(cm.stats(), indent=2)}")

    elif cmd == "topology":
        print(json.dumps(cm.topology(), indent=2))

    elif cmd == "stats":
        print(json.dumps(cm.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
