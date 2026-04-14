#!/usr/bin/env python3
"""
jarvis_prompt_library — Versioned prompt template library with variable substitution
Stores, retrieves, and renders prompt templates with A/B variant support
"""

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.prompt_library")

LIBRARY_FILE = Path("/home/turbo/IA/Core/jarvis/data/prompt_library.json")
REDIS_PREFIX = "jarvis:prompts:"

_VAR_RE = re.compile(r"\{\{(\w+)\}\}")


class PromptCategory(str, Enum):
    SYSTEM = "system"
    USER = "user"
    CHAIN = "chain"
    TOOL = "tool"
    EVAL = "eval"
    CUSTOM = "custom"


@dataclass
class PromptVariant:
    variant_id: str
    template: str
    weight: float = 1.0  # for A/B sampling
    description: str = ""
    metadata: dict = field(default_factory=dict)

    def variables(self) -> list[str]:
        return list(dict.fromkeys(_VAR_RE.findall(self.template)))

    def render(self, **kwargs: Any) -> str:
        result = self.template
        for var in self.variables():
            val = str(kwargs.get(var, f"{{{{{var}}}}}"))
            result = result.replace(f"{{{{{var}}}}}", val)
        return result

    def to_dict(self) -> dict:
        return {
            "variant_id": self.variant_id,
            "template": self.template,
            "weight": self.weight,
            "description": self.description,
            "variables": self.variables(),
        }


@dataclass
class PromptTemplate:
    prompt_id: str
    name: str
    category: PromptCategory
    variants: list[PromptVariant] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    version: int = 1
    active: bool = True
    author: str = "system"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    use_count: int = 0

    def default_variant(self) -> PromptVariant | None:
        return self.variants[0] if self.variants else None

    def get_variant(self, variant_id: str) -> PromptVariant | None:
        for v in self.variants:
            if v.variant_id == variant_id:
                return v
        return None

    def sample_variant(self) -> PromptVariant | None:
        """Weighted random sample."""
        if not self.variants:
            return None
        import random

        weights = [v.weight for v in self.variants]
        total = sum(weights)
        r = random.uniform(0, total)
        cumulative = 0.0
        for v, w in zip(self.variants, weights):
            cumulative += w
            if r <= cumulative:
                return v
        return self.variants[-1]

    def render(self, variant_id: str | None = None, **kwargs: Any) -> str:
        if variant_id:
            v = self.get_variant(variant_id)
        else:
            v = self.sample_variant()
        if not v:
            raise KeyError(f"No variant found for prompt {self.prompt_id!r}")
        self.use_count += 1
        self.updated_at = time.time()
        return v.render(**kwargs)

    def checksum(self) -> str:
        content = json.dumps([v.template for v in self.variants], sort_keys=True)
        return hashlib.md5(content.encode()).hexdigest()[:8]

    def to_dict(self, include_variants: bool = True) -> dict:
        d = {
            "prompt_id": self.prompt_id,
            "name": self.name,
            "category": self.category.value,
            "version": self.version,
            "active": self.active,
            "tags": self.tags,
            "author": self.author,
            "use_count": self.use_count,
            "checksum": self.checksum(),
            "variant_count": len(self.variants),
        }
        if include_variants:
            d["variants"] = [v.to_dict() for v in self.variants]
        return d


class PromptLibrary:
    def __init__(self, persist: bool = True):
        self.redis: aioredis.Redis | None = None
        self._templates: dict[str, PromptTemplate] = {}
        self._persist = persist
        self._stats: dict[str, int] = {"renders": 0, "misses": 0}
        if persist:
            LIBRARY_FILE.parent.mkdir(parents=True, exist_ok=True)

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def add(
        self,
        prompt_id: str,
        name: str,
        template: str,
        category: PromptCategory = PromptCategory.USER,
        tags: list[str] | None = None,
        author: str = "system",
        description: str = "",
    ) -> PromptTemplate:
        variant = PromptVariant(
            variant_id="default",
            template=template,
            description=description,
        )
        existing = self._templates.get(prompt_id)
        if existing:
            existing.variants = [variant]
            existing.version += 1
            existing.updated_at = time.time()
            return existing

        tmpl = PromptTemplate(
            prompt_id=prompt_id,
            name=name,
            category=category,
            variants=[variant],
            tags=tags or [],
            author=author,
        )
        self._templates[prompt_id] = tmpl
        return tmpl

    def add_variant(
        self,
        prompt_id: str,
        variant_id: str,
        template: str,
        weight: float = 1.0,
        description: str = "",
    ) -> PromptVariant:
        tmpl = self._templates.get(prompt_id)
        if not tmpl:
            raise KeyError(f"Prompt {prompt_id!r} not found")
        variant = PromptVariant(
            variant_id=variant_id,
            template=template,
            weight=weight,
            description=description,
        )
        # Remove existing variant with same id
        tmpl.variants = [v for v in tmpl.variants if v.variant_id != variant_id]
        tmpl.variants.append(variant)
        tmpl.version += 1
        return variant

    def get(self, prompt_id: str) -> PromptTemplate | None:
        return self._templates.get(prompt_id)

    def render(
        self, prompt_id: str, variant_id: str | None = None, **kwargs: Any
    ) -> str:
        tmpl = self._templates.get(prompt_id)
        if not tmpl:
            self._stats["misses"] += 1
            raise KeyError(f"Prompt {prompt_id!r} not found")
        self._stats["renders"] += 1
        result = tmpl.render(variant_id=variant_id, **kwargs)
        if self.redis:
            asyncio.create_task(self._redis_record(prompt_id))
        return result

    async def _redis_record(self, prompt_id: str):
        if not self.redis:
            return
        try:
            await self.redis.hincrby(f"{REDIS_PREFIX}usage", prompt_id, 1)
        except Exception:
            pass

    def search(
        self,
        query: str = "",
        category: PromptCategory | None = None,
        tags: list[str] | None = None,
    ) -> list[PromptTemplate]:
        results = list(self._templates.values())
        if category:
            results = [t for t in results if t.category == category]
        if tags:
            results = [t for t in results if any(tag in t.tags for tag in tags)]
        if query:
            q = query.lower()
            results = [
                t for t in results if q in t.name.lower() or q in t.prompt_id.lower()
            ]
        return results

    def save(self):
        try:
            data = {pid: t.to_dict() for pid, t in self._templates.items()}
            LIBRARY_FILE.write_text(json.dumps(data, indent=2))
        except Exception as e:
            log.warning(f"Save failed: {e}")

    def list_all(self) -> list[dict]:
        return [t.to_dict(include_variants=False) for t in self._templates.values()]

    def stats(self) -> dict:
        return {
            **self._stats,
            "templates": len(self._templates),
            "total_variants": sum(len(t.variants) for t in self._templates.values()),
        }


def build_jarvis_prompt_library() -> PromptLibrary:
    lib = PromptLibrary(persist=True)

    lib.add(
        "system.jarvis",
        "JARVIS System Prompt",
        "You are JARVIS, an intelligent multi-agent orchestration system. "
        "You manage GPU clusters, LLM inference, trading pipelines, and automation workflows. "
        "Be precise, concise, and action-oriented.",
        category=PromptCategory.SYSTEM,
        tags=["core", "system"],
    )
    lib.add(
        "inference.router",
        "Inference Router",
        "Given the following task: {{task}}\n\n"
        "Select the best model from: {{models}}\n"
        "Consider: latency={{latency_req}}, quality={{quality_req}}, cost={{cost_req}}\n"
        'Respond with: {"model": "...", "reason": "..."}',
        category=PromptCategory.TOOL,
        tags=["routing", "inference"],
    )
    lib.add(
        "trading.analysis",
        "Trading Analysis",
        "Analyze {{symbol}} on {{timeframe}} timeframe.\n"
        "Current price: {{price}}\n"
        "Recent data: {{ohlcv}}\n\n"
        "Provide: trend, support/resistance, entry/exit points, risk assessment.",
        category=PromptCategory.USER,
        tags=["trading", "analysis"],
    )
    lib.add(
        "eval.quality",
        "Response Quality Evaluator",
        "Evaluate the following AI response for quality.\n\n"
        "Question: {{question}}\nResponse: {{response}}\n\n"
        "Score 1-10 on: accuracy, completeness, clarity. "
        'Respond as JSON: {"accuracy": N, "completeness": N, "clarity": N, "notes": "..."}',
        category=PromptCategory.EVAL,
        tags=["eval", "quality"],
    )

    # A/B variant for trading
    lib.add_variant(
        "trading.analysis",
        "concise",
        "{{symbol}} @ {{price}} ({{timeframe}}): Quick analysis — trend, key levels, action.",
        weight=0.3,
        description="Shorter variant for fast scanning",
    )

    return lib


async def main():
    import sys

    lib = build_jarvis_prompt_library()
    await lib.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Prompt library demo...")

        rendered = lib.render(
            "inference.router",
            task="Summarize a 10-page document",
            models="qwen3.5-27b, deepseek-r1, gemma3:4b",
            latency_req="<3s",
            quality_req="high",
            cost_req="low",
        )
        print(f"\nRendered 'inference.router':\n{rendered}")

        rendered2 = lib.render(
            "trading.analysis",
            symbol="BTC/USDT",
            timeframe="4h",
            price="67500",
            ohlcv="[...]",
        )
        print(
            f"\nRendered 'trading.analysis' (variant chosen by weight):\n{rendered2[:120]}..."
        )

        print("\nAll prompts:")
        for p in lib.list_all():
            print(
                f"  {p['prompt_id']:<25} v{p['version']} variants={p['variant_count']} uses={p['use_count']}"
            )

        print(f"\nStats: {json.dumps(lib.stats(), indent=2)}")

    elif cmd == "list":
        for p in lib.list_all():
            print(json.dumps(p))

    elif cmd == "stats":
        print(json.dumps(lib.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
