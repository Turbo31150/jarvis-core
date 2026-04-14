#!/usr/bin/env python3
"""
jarvis_model_aliaser — Model name aliasing and normalization for the JARVIS cluster
Maps short names, versions, and aliases to canonical model IDs with endpoint resolution
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.model_aliaser")

REDIS_PREFIX = "jarvis:alias:"
ALIAS_FILE = Path("/home/turbo/IA/Core/jarvis/data/model_aliases.json")


@dataclass
class ModelDescriptor:
    canonical_id: str  # e.g. "qwen3.5-27b-instruct"
    aliases: list[str]  # e.g. ["qwen27b", "qwen3.5-27b", "big"]
    endpoint_url: str  # e.g. "http://192.168.1.85:1234"
    context_window: int = 32768
    supports_tools: bool = True
    supports_vision: bool = False
    is_local: bool = True
    weight: float = 1.0
    tags: list[str] = field(default_factory=list)
    added_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "canonical_id": self.canonical_id,
            "aliases": self.aliases,
            "endpoint_url": self.endpoint_url,
            "context_window": self.context_window,
            "supports_tools": self.supports_tools,
            "supports_vision": self.supports_vision,
            "is_local": self.is_local,
            "weight": self.weight,
            "tags": self.tags,
        }


class ModelAliaser:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._models: dict[str, ModelDescriptor] = {}  # canonical_id → descriptor
        self._alias_map: dict[str, str] = {}  # alias → canonical_id
        self._stats: dict[str, int] = {
            "lookups": 0,
            "hits": 0,
            "misses": 0,
            "registrations": 0,
        }
        ALIAS_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._load()
        self._register_defaults()

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def _load(self):
        if not ALIAS_FILE.exists():
            return
        try:
            data = json.loads(ALIAS_FILE.read_text())
            for d in data.get("models", []):
                desc = ModelDescriptor(
                    canonical_id=d["canonical_id"],
                    aliases=d.get("aliases", []),
                    endpoint_url=d.get("endpoint_url", ""),
                    context_window=d.get("context_window", 32768),
                    supports_tools=d.get("supports_tools", True),
                    supports_vision=d.get("supports_vision", False),
                    is_local=d.get("is_local", True),
                    weight=d.get("weight", 1.0),
                    tags=d.get("tags", []),
                )
                self._register_internal(desc)
        except Exception as e:
            log.warning(f"Alias file load error: {e}")

    def _save(self):
        try:
            data = {"models": [m.to_dict() for m in self._models.values()]}
            ALIAS_FILE.write_text(json.dumps(data, indent=2))
        except Exception as e:
            log.warning(f"Alias file save error: {e}")

    def _register_internal(self, desc: ModelDescriptor):
        self._models[desc.canonical_id] = desc
        self._alias_map[desc.canonical_id.lower()] = desc.canonical_id
        for alias in desc.aliases:
            self._alias_map[alias.lower()] = desc.canonical_id

    def _register_defaults(self):
        defaults = [
            ModelDescriptor(
                "qwen3.5-9b",
                ["qwen9b", "qwen-9b", "fast", "qwen3.5-9b-instruct"],
                "http://192.168.1.85:1234",
                context_window=32768,
                weight=1.2,
                tags=["fast", "local"],
            ),
            ModelDescriptor(
                "qwen3.5-27b",
                ["qwen27b", "qwen-27b", "medium", "qwen3.5-27b-instruct"],
                "http://192.168.1.85:1234",
                context_window=32768,
                weight=1.0,
                tags=["balanced", "local"],
            ),
            ModelDescriptor(
                "deepseek-r1-0528",
                ["deepseek", "r1", "reason", "deepseek-r1"],
                "http://192.168.1.26:1234",
                context_window=65536,
                weight=0.9,
                tags=["reasoning", "local"],
            ),
            ModelDescriptor(
                "gemma3:4b",
                ["gemma", "gemma3", "gemma-4b", "tiny", "ol1"],
                "http://127.0.0.1:11434",
                context_window=8192,
                weight=0.5,
                tags=["tiny", "local", "ollama"],
            ),
            ModelDescriptor(
                "nomic-embed-text",
                ["nomic", "embed", "nomic-embed", "embedding"],
                "http://192.168.1.26:1234",
                context_window=8192,
                supports_tools=False,
                weight=1.0,
                tags=["embedding", "local"],
            ),
            ModelDescriptor(
                "claude-sonnet-4-6",
                ["sonnet", "claude", "claude-sonnet"],
                "https://api.anthropic.com",
                context_window=200000,
                is_local=False,
                weight=0.3,
                tags=["cloud", "powerful"],
            ),
        ]
        for desc in defaults:
            if desc.canonical_id not in self._models:
                self._register_internal(desc)

    def register(self, desc: ModelDescriptor):
        self._register_internal(desc)
        self._stats["registrations"] += 1
        self._save()

    def resolve(self, name: str) -> ModelDescriptor | None:
        self._stats["lookups"] += 1
        canonical = self._alias_map.get(name.lower())
        if canonical:
            self._stats["hits"] += 1
            return self._models.get(canonical)
        self._stats["misses"] += 1
        log.debug(f"Model alias not found: {name}")
        return None

    def resolve_or_default(
        self, name: str, default: str = "qwen3.5-9b"
    ) -> ModelDescriptor:
        result = self.resolve(name)
        if result:
            return result
        return self._models.get(default) or next(iter(self._models.values()))

    def canonical(self, name: str) -> str:
        desc = self.resolve(name)
        return desc.canonical_id if desc else name

    def endpoint(self, name: str) -> str | None:
        desc = self.resolve(name)
        return desc.endpoint_url if desc else None

    def find_by_tag(self, tag: str) -> list[ModelDescriptor]:
        return [d for d in self._models.values() if tag in d.tags]

    def find_local(self) -> list[ModelDescriptor]:
        return [d for d in self._models.values() if d.is_local]

    def add_alias(self, canonical_id: str, alias: str):
        desc = self._models.get(canonical_id)
        if not desc:
            raise ValueError(f"Unknown canonical model: {canonical_id}")
        if alias not in desc.aliases:
            desc.aliases.append(alias)
        self._alias_map[alias.lower()] = canonical_id
        self._save()

    def list_models(self) -> list[dict]:
        return [m.to_dict() for m in self._models.values()]

    def stats(self) -> dict:
        return {
            **self._stats,
            "models": len(self._models),
            "aliases": len(self._alias_map),
        }


def build_jarvis_aliaser() -> ModelAliaser:
    return ModelAliaser()


async def main():
    import sys

    aliaser = build_jarvis_aliaser()
    await aliaser.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        queries = [
            "qwen9b",
            "fast",
            "deepseek",
            "r1",
            "reason",
            "gemma",
            "embed",
            "claude",
            "sonnet",
            "QWEN27B",
            "unknown-model",
        ]
        print("Alias resolution:")
        for q in queries:
            desc = aliaser.resolve(q)
            if desc:
                print(f"  {q:<20} → {desc.canonical_id:<35} @ {desc.endpoint_url}")
            else:
                print(f"  {q:<20} → [NOT FOUND]")

        print(f"\nLocal models: {[d.canonical_id for d in aliaser.find_local()]}")
        print(
            f"Reasoning models: {[d.canonical_id for d in aliaser.find_by_tag('reasoning')]}"
        )

        aliaser.add_alias("qwen3.5-9b", "q9")
        print(f"\nAfter adding alias 'q9': {aliaser.canonical('q9')}")

        print(f"\nStats: {json.dumps(aliaser.stats(), indent=2)}")

    elif cmd == "list":
        for m in aliaser.list_models():
            print(f"  {m['canonical_id']:<35} aliases={m['aliases']}")

    elif cmd == "stats":
        print(json.dumps(aliaser.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
