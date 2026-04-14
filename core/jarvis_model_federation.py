#!/usr/bin/env python3
"""
jarvis_model_federation — Federated model registry across M1+M2+OL1
Unified catalog: which model is on which node, VRAM cost, load/unload
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.model_federation")

NODES = {
    "M1": {"url": "http://127.0.0.1:1234", "ollama": False},
    "M2": {"url": "http://192.168.1.26:1234", "ollama": False},
    "OL1": {"url": "http://127.0.0.1:11434", "ollama": True},
}

# Known model VRAM footprints (GB) — approximations
MODEL_VRAM = {
    "qwen3.5-9b": 6.5,
    "qwen3.5-35b-a3b": 22.0,
    "qwen3.5-27b": 17.0,
    "qwen3-14b": 9.0,
    "qwen3-8b": 5.5,
    "deepseek-r1-0528-qwen3-8b": 5.5,
    "deepseek-r1": 8.0,
    "glm-4.7-flash": 5.0,
    "gpt-oss-20b": 13.0,
    "gemma-4-26b-a4b": 17.0,
    "nomic-embed": 0.3,
    "gemma3:4b": 3.0,
    "llama3.2": 2.5,
}

REDIS_KEY = "jarvis:federation"


@dataclass
class ModelEntry:
    model_id: str
    node: str
    url: str
    loaded: bool = True
    vram_gb: float = 0.0
    aliases: list[str] = field(default_factory=list)

    def matches(self, query: str) -> bool:
        q = query.lower()
        return q in self.model_id.lower() or any(q in a.lower() for a in self.aliases)


class ModelFederation:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self.catalog: list[ModelEntry] = []
        self.last_sync: float = 0.0

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    async def _fetch_node_models(self, name: str, cfg: dict) -> list[ModelEntry]:
        entries = []
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5)
            ) as sess:
                if cfg["ollama"]:
                    async with sess.get(f"{cfg['url']}/api/tags") as r:
                        if r.status != 200:
                            return entries
                        data = await r.json()
                        models = [m["name"] for m in data.get("models", [])]
                else:
                    async with sess.get(f"{cfg['url']}/v1/models") as r:
                        if r.status != 200:
                            return entries
                        data = await r.json()
                        models = [m["id"] for m in data.get("data", [])]

            for mid in models:
                # Estimate VRAM
                vram = 0.0
                for key, gb in MODEL_VRAM.items():
                    if key.lower() in mid.lower():
                        vram = gb
                        break

                entries.append(
                    ModelEntry(
                        model_id=mid,
                        node=name,
                        url=cfg["url"],
                        loaded=True,
                        vram_gb=vram,
                    )
                )
        except Exception as e:
            log.debug(f"Fetch {name}: {e}")
        return entries

    async def sync(self) -> int:
        tasks = [self._fetch_node_models(n, cfg) for n, cfg in NODES.items()]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        self.catalog = []
        for r in results:
            if not isinstance(r, Exception):
                self.catalog.extend(r)
        self.last_sync = time.time()

        if self.redis:
            await self.redis.set(
                REDIS_KEY,
                json.dumps(self._to_dict()),
                ex=120,
            )

        return len(self.catalog)

    def find(self, query: str) -> list[ModelEntry]:
        return [e for e in self.catalog if e.matches(query)]

    def by_node(self, node: str) -> list[ModelEntry]:
        return [e for e in self.catalog if e.node == node]

    def total_vram_by_node(self) -> dict[str, float]:
        result: dict[str, float] = {}
        for e in self.catalog:
            result[e.node] = round(result.get(e.node, 0.0) + e.vram_gb, 1)
        return result

    def best_node_for_model(self, query: str) -> str | None:
        matches = self.find(query)
        if not matches:
            return None
        # Prefer M1 (local), then M2, then OL1
        priority = {"M1": 0, "M2": 1, "OL1": 2}
        return min(matches, key=lambda e: priority.get(e.node, 9)).node

    def _to_dict(self) -> dict:
        return {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "total": len(self.catalog),
            "by_node": {n: [e.model_id for e in self.by_node(n)] for n in NODES},
            "vram_by_node": self.total_vram_by_node(),
        }

    async def report(self) -> dict:
        if time.time() - self.last_sync > 60:
            await self.sync()
        return self._to_dict()


# ── CLI ───────────────────────────────────────────────────────────────────────


async def main():
    import sys

    fed = ModelFederation()
    await fed.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"

    if cmd == "list":
        n = await fed.sync()
        r = fed._to_dict()
        print(f"Federation: {n} models across {len(NODES)} nodes\n")
        for node, models in r["by_node"].items():
            vram = r["vram_by_node"].get(node, 0)
            print(f"  {node} ({vram}GB VRAM):")
            for m in models:
                print(f"    • {m}")

    elif cmd == "find" and len(sys.argv) > 2:
        await fed.sync()
        query = sys.argv[2]
        matches = fed.find(query)
        if matches:
            for e in matches:
                print(f"  [{e.node}] {e.model_id} (~{e.vram_gb}GB)")
        else:
            print(f"No model matching '{query}'")

    elif cmd == "best" and len(sys.argv) > 2:
        await fed.sync()
        node = fed.best_node_for_model(sys.argv[2])
        print(f"Best node for '{sys.argv[2]}': {node or 'NOT FOUND'}")

    elif cmd == "vram":
        await fed.sync()
        vram = fed.total_vram_by_node()
        for node, gb in vram.items():
            print(f"  {node}: {gb}GB loaded")


if __name__ == "__main__":
    asyncio.run(main())
