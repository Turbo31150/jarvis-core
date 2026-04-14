#!/usr/bin/env python3
"""
jarvis_model_catalog — Centralized model registry with capabilities and routing metadata
Tracks all available models across nodes with performance profiles and capability tags
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.model_catalog")

CATALOG_FILE = Path("/home/turbo/IA/Core/jarvis/data/model_catalog.json")
REDIS_KEY = "jarvis:model_catalog"

NODES = {
    "M1": "http://127.0.0.1:1234",
    "M2": "http://192.168.1.26:1234",
    "OL1": "http://127.0.0.1:11434",
}

# Manual capability metadata (augments auto-discovery)
MODEL_META: dict[str, dict] = {
    "qwen3.5-9b": {
        "capabilities": ["chat", "code", "reasoning", "tool_use"],
        "context_length": 32768,
        "vram_mb": 5800,
        "tier": "standard",
        "speed": "fast",
    },
    "qwen3.5-27b-claude": {
        "capabilities": [
            "chat",
            "code",
            "reasoning",
            "creative",
            "tool_use",
            "distilled",
        ],
        "context_length": 32768,
        "vram_mb": 16000,
        "tier": "premium",
        "speed": "medium",
    },
    "qwen3.5-35b-a3b": {
        "capabilities": ["chat", "code", "reasoning", "creative"],
        "context_length": 32768,
        "vram_mb": 22000,
        "tier": "premium",
        "speed": "slow",
    },
    "deepseek-r1-0528": {
        "capabilities": ["reasoning", "math", "code", "analysis"],
        "context_length": 65536,
        "vram_mb": 28000,
        "tier": "specialist",
        "speed": "slow",
    },
    "glm-4.7-flash-claude": {
        "capabilities": ["chat", "code", "distilled"],
        "context_length": 8192,
        "vram_mb": 5000,
        "tier": "fast",
        "speed": "fast",
    },
    "nomic-embed": {
        "capabilities": ["embedding"],
        "context_length": 8192,
        "vram_mb": 600,
        "tier": "utility",
        "speed": "fast",
    },
}


@dataclass
class ModelRecord:
    model_id: str
    node: str
    node_url: str
    capabilities: list[str] = field(default_factory=list)
    context_length: int = 4096
    vram_mb: int = 0
    tier: str = "standard"  # fast | standard | premium | specialist | utility
    speed: str = "medium"  # fast | medium | slow
    available: bool = True
    last_seen: float = field(default_factory=time.time)
    avg_latency_ms: float = 0.0
    avg_tok_per_s: float = 0.0
    request_count: int = 0

    def matches_capability(self, cap: str) -> bool:
        return cap.lower() in [c.lower() for c in self.capabilities]

    def to_dict(self) -> dict:
        return {
            "model_id": self.model_id,
            "node": self.node,
            "node_url": self.node_url,
            "capabilities": self.capabilities,
            "context_length": self.context_length,
            "vram_mb": self.vram_mb,
            "tier": self.tier,
            "speed": self.speed,
            "available": self.available,
            "last_seen": self.last_seen,
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "avg_tok_per_s": round(self.avg_tok_per_s, 1),
            "request_count": self.request_count,
        }


class ModelCatalog:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._models: dict[str, ModelRecord] = {}  # "{node}:{model_id}" → record
        self._load()

    def _load(self):
        if CATALOG_FILE.exists():
            try:
                data = json.loads(CATALOG_FILE.read_text())
                for d in data.get("models", []):
                    key = f"{d['node']}:{d['model_id']}"
                    self._models[key] = ModelRecord(
                        **{
                            k: v
                            for k, v in d.items()
                            if k in ModelRecord.__dataclass_fields__
                        }
                    )
                log.debug(f"Loaded {len(self._models)} model records")
            except Exception as e:
                log.warning(f"Catalog load error: {e}")

    def _save(self):
        CATALOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CATALOG_FILE.write_text(
            json.dumps(
                {
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "models": [m.to_dict() for m in self._models.values()],
                },
                indent=2,
            )
        )

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    async def discover(self) -> int:
        """Query all nodes and update catalog."""
        count = 0
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5)
        ) as sess:
            for node, url in NODES.items():
                try:
                    # LM Studio / OpenAI-compatible
                    endpoint = (
                        f"{url}/v1/models" if node != "OL1" else f"{url}/api/tags"
                    )
                    async with sess.get(endpoint) as r:
                        if r.status != 200:
                            continue
                        data = await r.json()
                        if node == "OL1":
                            model_ids = [m["name"] for m in data.get("models", [])]
                        else:
                            model_ids = [m["id"] for m in data.get("data", [])]

                        for mid in model_ids:
                            key = f"{node}:{mid}"
                            meta = self._find_meta(mid)
                            existing = self._models.get(key)
                            if existing:
                                existing.available = True
                                existing.last_seen = time.time()
                                existing.capabilities = meta.get(
                                    "capabilities", existing.capabilities
                                )
                            else:
                                self._models[key] = ModelRecord(
                                    model_id=mid,
                                    node=node,
                                    node_url=url,
                                    capabilities=meta.get("capabilities", ["chat"]),
                                    context_length=meta.get("context_length", 4096),
                                    vram_mb=meta.get("vram_mb", 0),
                                    tier=meta.get("tier", "standard"),
                                    speed=meta.get("speed", "medium"),
                                )
                            count += 1
                except Exception as e:
                    log.debug(f"Discovery error on {node}: {e}")
                    # Mark node models as unavailable
                    for key, m in self._models.items():
                        if m.node == node:
                            m.available = False

        self._save()
        if self.redis:
            await self.redis.set(
                REDIS_KEY,
                json.dumps([m.to_dict() for m in self._models.values()]),
                ex=300,
            )
        log.info(f"Discovered {count} models across {len(NODES)} nodes")
        return count

    def _find_meta(self, model_id: str) -> dict:
        for key, meta in MODEL_META.items():
            if key.lower() in model_id.lower():
                return meta
        return {}

    def find(
        self,
        capability: str | None = None,
        tier: str | None = None,
        speed: str | None = None,
        node: str | None = None,
        available_only: bool = True,
    ) -> list[ModelRecord]:
        results = list(self._models.values())
        if available_only:
            results = [m for m in results if m.available]
        if capability:
            results = [m for m in results if m.matches_capability(capability)]
        if tier:
            results = [m for m in results if m.tier == tier]
        if speed:
            results = [m for m in results if m.speed == speed]
        if node:
            results = [m for m in results if m.node == node]
        return sorted(results, key=lambda m: m.avg_latency_ms or 9999)

    def best_for(self, task: str) -> ModelRecord | None:
        """Select best available model for a task type."""
        task_caps = {
            "chat": "chat",
            "code": "code",
            "reasoning": "reasoning",
            "embedding": "embedding",
            "creative": "creative",
            "fast": None,
        }
        cap = task_caps.get(task.lower())
        candidates = self.find(capability=cap) if cap else self.find()
        if task == "fast":
            candidates = self.find(speed="fast")
        return candidates[0] if candidates else None

    def update_stats(
        self, model_id: str, node: str, latency_ms: float, tok_per_s: float
    ):
        key = f"{node}:{model_id}"
        m = self._models.get(key)
        if m:
            alpha = 0.2
            m.avg_latency_ms = alpha * latency_ms + (1 - alpha) * (
                m.avg_latency_ms or latency_ms
            )
            m.avg_tok_per_s = alpha * tok_per_s + (1 - alpha) * (
                m.avg_tok_per_s or tok_per_s
            )
            m.request_count += 1

    def list_all(self, available_only: bool = False) -> list[dict]:
        models = list(self._models.values())
        if available_only:
            models = [m for m in models if m.available]
        return [m.to_dict() for m in sorted(models, key=lambda x: (x.node, x.model_id))]

    def stats(self) -> dict:
        all_m = list(self._models.values())
        return {
            "total": len(all_m),
            "available": sum(1 for m in all_m if m.available),
            "nodes": list(NODES.keys()),
            "by_node": {
                node: sum(1 for m in all_m if m.node == node and m.available)
                for node in NODES
            },
            "by_tier": {
                tier: sum(1 for m in all_m if m.tier == tier and m.available)
                for tier in ["fast", "standard", "premium", "specialist", "utility"]
            },
        }


async def main():
    import sys

    catalog = ModelCatalog()
    await catalog.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"

    if cmd == "discover":
        count = await catalog.discover()
        print(f"Discovered {count} models")
        s = catalog.stats()
        print(f"Available: {s['available']}/{s['total']}")
        for node, cnt in s["by_node"].items():
            print(f"  {node}: {cnt} models")

    elif cmd == "list":
        avail = "--all" not in sys.argv
        models = catalog.list_all(available_only=avail)
        print(f"{'Node':<6} {'Model':<45} {'Tier':<12} {'Speed':<8} {'Capabilities'}")
        print("-" * 90)
        for m in models:
            status = "✅" if m["available"] else "❌"
            caps = ", ".join(m["capabilities"][:4])
            print(
                f"  {status} {m['node']:<6} {m['model_id']:<45} {m['tier']:<12} {m['speed']:<8} {caps}"
            )

    elif cmd == "find" and len(sys.argv) > 2:
        cap = sys.argv[2]
        results = catalog.find(capability=cap)
        print(f"Models with '{cap}' capability:")
        for m in results:
            print(f"  {m.node}:{m.model_id} ({m.tier}, {m.speed})")

    elif cmd == "best" and len(sys.argv) > 2:
        task = sys.argv[2]
        m = catalog.best_for(task)
        if m:
            print(f"Best for '{task}': {m.node}:{m.model_id}")
        else:
            print(f"No model found for task '{task}'")


if __name__ == "__main__":
    asyncio.run(main())
