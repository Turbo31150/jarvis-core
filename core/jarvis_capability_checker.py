#!/usr/bin/env python3
"""
jarvis_capability_checker — Runtime capability discovery and validation for cluster nodes
Probes endpoints for supported models, features, and API versions
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

log = logging.getLogger("jarvis.capability_checker")

REDIS_PREFIX = "jarvis:caps:"


class Capability(str, Enum):
    CHAT = "chat"
    COMPLETIONS = "completions"
    EMBEDDINGS = "embeddings"
    VISION = "vision"
    FUNCTION_CALLING = "function_calling"
    STREAMING = "streaming"
    JSON_MODE = "json_mode"
    RERANKING = "reranking"
    WHISPER = "whisper"
    TTS = "tts"


@dataclass
class NodeCapabilities:
    node_id: str
    endpoint_url: str
    capabilities: set[Capability] = field(default_factory=set)
    models: list[str] = field(default_factory=list)
    api_version: str = ""
    max_tokens: int = 0
    context_length: int = 0
    supports_batch: bool = False
    last_checked: float = 0.0
    check_latency_ms: float = 0.0
    online: bool = False
    error: str = ""

    @property
    def stale(self) -> bool:
        return (time.time() - self.last_checked) > 300.0

    def has(self, cap: Capability) -> bool:
        return cap in self.capabilities

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "endpoint_url": self.endpoint_url,
            "capabilities": sorted(c.value for c in self.capabilities),
            "models": self.models,
            "api_version": self.api_version,
            "max_tokens": self.max_tokens,
            "context_length": self.context_length,
            "supports_batch": self.supports_batch,
            "last_checked": self.last_checked,
            "check_latency_ms": round(self.check_latency_ms, 1),
            "online": self.online,
            "error": self.error,
        }


@dataclass
class CapabilityRequirement:
    capabilities: list[Capability]
    model_pattern: str = ""  # substring match
    min_context: int = 0
    prefer_online: bool = True

    def satisfied_by(self, node: NodeCapabilities) -> bool:
        if self.prefer_online and not node.online:
            return False
        for cap in self.capabilities:
            if not node.has(cap):
                return False
        if self.model_pattern:
            if not any(self.model_pattern.lower() in m.lower() for m in node.models):
                return False
        if self.min_context and node.context_length < self.min_context:
            return False
        return True


async def _probe_lmstudio(
    node_id: str, endpoint_url: str, timeout_s: float = 10.0
) -> NodeCapabilities:
    caps = NodeCapabilities(node_id=node_id, endpoint_url=endpoint_url)
    t0 = time.time()
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout_s)
        ) as sess:
            # /v1/models
            async with sess.get(f"{endpoint_url}/v1/models") as r:
                caps.check_latency_ms = (time.time() - t0) * 1000
                if r.status != 200:
                    caps.error = f"HTTP {r.status}"
                    return caps
                data = await r.json(content_type=None)

            models = [m.get("id", "") for m in data.get("data", [])]
            caps.models = [m for m in models if m]
            caps.online = True
            caps.last_checked = time.time()
            caps.api_version = "v1"

            # Infer capabilities from model names
            caps.capabilities.add(Capability.CHAT)
            caps.capabilities.add(Capability.STREAMING)

            for m in caps.models:
                ml = m.lower()
                if "embed" in ml or "nomic" in ml or "bge" in ml:
                    caps.capabilities.add(Capability.EMBEDDINGS)
                if "vision" in ml or "vl" in ml or "llava" in ml:
                    caps.capabilities.add(Capability.VISION)
                if "whisper" in ml:
                    caps.capabilities.add(Capability.WHISPER)
                if "tts" in ml:
                    caps.capabilities.add(Capability.TTS)

            # Probe embeddings endpoint
            try:
                emb_payload = {
                    "model": caps.models[0] if caps.models else "",
                    "input": "test",
                }
                async with sess.post(
                    f"{endpoint_url}/v1/embeddings", json=emb_payload
                ) as r2:
                    if r2.status == 200:
                        caps.capabilities.add(Capability.EMBEDDINGS)
            except Exception:
                pass

            # Probe function calling via smoke chat
            if caps.models:
                fc_payload = {
                    "model": caps.models[0],
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 1,
                    "tools": [
                        {
                            "type": "function",
                            "function": {"name": "test", "parameters": {}},
                        }
                    ],
                }
                try:
                    async with sess.post(
                        f"{endpoint_url}/v1/chat/completions", json=fc_payload
                    ) as r3:
                        if r3.status == 200:
                            caps.capabilities.add(Capability.FUNCTION_CALLING)
                except Exception:
                    pass

    except Exception as e:
        caps.error = str(e)[:200]
        caps.check_latency_ms = (time.time() - t0) * 1000

    return caps


async def _probe_ollama(
    node_id: str, endpoint_url: str, timeout_s: float = 10.0
) -> NodeCapabilities:
    caps = NodeCapabilities(node_id=node_id, endpoint_url=endpoint_url)
    t0 = time.time()
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout_s)
        ) as sess:
            async with sess.get(f"{endpoint_url}/api/tags") as r:
                caps.check_latency_ms = (time.time() - t0) * 1000
                if r.status != 200:
                    caps.error = f"HTTP {r.status}"
                    return caps
                data = await r.json(content_type=None)

            caps.models = [m.get("name", "") for m in data.get("models", [])]
            caps.online = True
            caps.last_checked = time.time()
            caps.api_version = "ollama"
            caps.capabilities.update([Capability.CHAT, Capability.STREAMING])

    except Exception as e:
        caps.error = str(e)[:200]
        caps.check_latency_ms = (time.time() - t0) * 1000

    return caps


class CapabilityChecker:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._nodes: dict[str, NodeCapabilities] = {}
        self._probe_fns: dict[str, Any] = {}  # node_id → probe function
        self._stats: dict[str, int] = {
            "probes_run": 0,
            "online": 0,
            "offline": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def register_lmstudio(self, node_id: str, endpoint_url: str):
        self._probe_fns[node_id] = ("lmstudio", endpoint_url)

    def register_ollama(self, node_id: str, endpoint_url: str):
        self._probe_fns[node_id] = ("ollama", endpoint_url)

    async def probe(self, node_id: str, force: bool = False) -> NodeCapabilities:
        existing = self._nodes.get(node_id)
        if existing and not existing.stale and not force:
            return existing

        entry = self._probe_fns.get(node_id)
        if not entry:
            return NodeCapabilities(
                node_id=node_id, endpoint_url="", error="Not registered"
            )

        kind, url = entry
        if kind == "lmstudio":
            caps = await _probe_lmstudio(node_id, url)
        else:
            caps = await _probe_ollama(node_id, url)

        self._nodes[node_id] = caps
        self._stats["probes_run"] += 1
        if caps.online:
            self._stats["online"] += 1
        else:
            self._stats["offline"] += 1

        if self.redis:
            asyncio.create_task(
                self.redis.setex(
                    f"{REDIS_PREFIX}{node_id}",
                    600,
                    json.dumps(caps.to_dict()),
                )
            )
        return caps

    async def probe_all(self, concurrency: int = 4) -> dict[str, NodeCapabilities]:
        sem = asyncio.Semaphore(concurrency)

        async def bounded(nid: str):
            async with sem:
                return await self.probe(nid, force=True)

        await asyncio.gather(*[bounded(nid) for nid in self._probe_fns])
        return self._nodes

    def find_nodes(self, req: CapabilityRequirement) -> list[NodeCapabilities]:
        matching = [n for n in self._nodes.values() if req.satisfied_by(n)]
        return sorted(matching, key=lambda n: -n.check_latency_ms)

    def get(self, node_id: str) -> NodeCapabilities | None:
        return self._nodes.get(node_id)

    def all_caps(self) -> list[dict]:
        return [n.to_dict() for n in self._nodes.values()]

    def stats(self) -> dict:
        return {**self._stats, "registered_nodes": len(self._probe_fns)}


def build_jarvis_capability_checker() -> CapabilityChecker:
    cc = CapabilityChecker()
    cc.register_lmstudio("m1", "http://192.168.1.85:1234")
    cc.register_lmstudio("m2", "http://192.168.1.26:1234")
    cc.register_ollama("ol1", "http://127.0.0.1:11434")
    return cc


async def main():
    import sys

    cc = build_jarvis_capability_checker()
    await cc.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "probe"

    if cmd == "probe":
        print("Probing all nodes...")
        await cc.probe_all()

        print("\nNode capabilities:")
        for n in cc.all_caps():
            status = "✅" if n["online"] else "❌"
            print(
                f"  {status} {n['node_id']:<6} "
                f"models={len(n['models'])} "
                f"caps={','.join(n['capabilities'][:4])} "
                f"latency={n['check_latency_ms']:.0f}ms"
            )
            for m in n["models"][:3]:
                print(f"       - {m}")

        # Find nodes with embeddings
        req = CapabilityRequirement(capabilities=[Capability.EMBEDDINGS])
        embed_nodes = cc.find_nodes(req)
        print(f"\nNodes with embeddings: {[n.node_id for n in embed_nodes]}")

        print(f"\nStats: {json.dumps(cc.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(cc.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
