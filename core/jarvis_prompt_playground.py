#!/usr/bin/env python3
"""
jarvis_prompt_playground — Interactive prompt testing and iteration environment
Compare prompt variants, track improvements, save best versions to library
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.prompt_playground")

PLAYGROUND_DIR = Path("/home/turbo/IA/Core/jarvis/data/playground")
LM_URL = "http://127.0.0.1:1234"
DEFAULT_MODEL = "qwen/qwen3.5-9b"


@dataclass
class PromptVariant:
    variant_id: str
    name: str
    system: str
    template: str  # may contain {variables}
    model: str = DEFAULT_MODEL
    temperature: float = 0.7
    max_tokens: int = 512
    tags: list[str] = field(default_factory=list)
    notes: str = ""
    created_at: float = field(default_factory=time.time)

    def render(self, variables: dict | None = None) -> str:
        if variables:
            return self.template.format(**variables)
        return self.template

    def to_dict(self) -> dict:
        return {
            "variant_id": self.variant_id,
            "name": self.name,
            "system": self.system,
            "template": self.template,
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "tags": self.tags,
            "notes": self.notes,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PromptVariant":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class PlaygroundRun:
    run_id: str
    variant_id: str
    variables: dict
    response: str
    latency_ms: float
    tokens_out: int
    score: float = 0.0  # manual or LLM-judged score 1-5
    notes: str = ""
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "variant_id": self.variant_id,
            "variables": self.variables,
            "response": self.response[:500],
            "latency_ms": round(self.latency_ms, 1),
            "tokens_out": self.tokens_out,
            "score": self.score,
            "notes": self.notes,
            "ts": self.ts,
        }


class PromptPlayground:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._variants: dict[str, PromptVariant] = {}
        self._runs: list[PlaygroundRun] = []
        PLAYGROUND_DIR.mkdir(parents=True, exist_ok=True)
        self._load_library()

    def _load_library(self):
        lib = PLAYGROUND_DIR / "library.json"
        if lib.exists():
            try:
                data = json.loads(lib.read_text())
                for d in data.get("variants", []):
                    v = PromptVariant.from_dict(d)
                    self._variants[v.variant_id] = v
                log.debug(f"Loaded {len(self._variants)} prompt variants")
            except Exception as e:
                log.warning(f"Load library error: {e}")

    def _save_library(self):
        lib = PLAYGROUND_DIR / "library.json"
        lib.write_text(
            json.dumps(
                {"variants": [v.to_dict() for v in self._variants.values()]},
                indent=2,
            )
        )

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def add_variant(
        self,
        name: str,
        template: str,
        system: str = "You are JARVIS, a helpful AI assistant.",
        model: str = DEFAULT_MODEL,
        temperature: float = 0.7,
        max_tokens: int = 512,
        tags: list[str] | None = None,
        notes: str = "",
    ) -> PromptVariant:
        variant = PromptVariant(
            variant_id=str(uuid.uuid4())[:8],
            name=name,
            system=system,
            template=template,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            tags=tags or [],
            notes=notes,
        )
        self._variants[variant.variant_id] = variant
        self._save_library()
        log.info(f"Added variant: {name} [{variant.variant_id}]")
        return variant

    async def run(
        self,
        variant_id: str,
        variables: dict | None = None,
    ) -> PlaygroundRun:
        variant = self._variants.get(variant_id)
        if not variant:
            raise ValueError(f"Variant '{variant_id}' not found")

        prompt = variant.render(variables)
        messages = []
        if variant.system:
            messages.append({"role": "system", "content": variant.system})
        messages.append({"role": "user", "content": prompt})

        t0 = time.time()
        response = ""
        tokens_out = 0

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=90)
            ) as sess:
                async with sess.post(
                    f"{LM_URL}/v1/chat/completions",
                    json={
                        "model": variant.model,
                        "messages": messages,
                        "max_tokens": variant.max_tokens,
                        "temperature": variant.temperature,
                    },
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        response = data["choices"][0]["message"]["content"].strip()
                        tokens_out = data.get("usage", {}).get(
                            "completion_tokens", len(response.split())
                        )
        except Exception as e:
            log.error(f"Run error: {e}")
            response = f"ERROR: {e}"

        latency_ms = (time.time() - t0) * 1000
        run = PlaygroundRun(
            run_id=str(uuid.uuid4())[:8],
            variant_id=variant_id,
            variables=variables or {},
            response=response,
            latency_ms=latency_ms,
            tokens_out=tokens_out,
        )
        self._runs.append(run)
        self._save_run(run, variant)
        return run

    async def compare(
        self,
        variant_ids: list[str],
        variables: dict | None = None,
    ) -> list[PlaygroundRun]:
        """Run multiple variants with same input, compare side by side."""
        tasks = [self.run(vid, variables) for vid in variant_ids]
        return await asyncio.gather(*tasks)

    async def iterate(
        self,
        base_variant_id: str,
        feedback: str,
        variables: dict | None = None,
    ) -> PromptVariant:
        """Use LLM to suggest an improved prompt based on feedback."""
        base = self._variants.get(base_variant_id)
        if not base:
            raise ValueError(f"Variant '{base_variant_id}' not found")

        improve_prompt = (
            f"Improve this prompt template based on the feedback.\n\n"
            f"Current template:\n{base.template}\n\n"
            f"Feedback: {feedback}\n\n"
            f"Return only the improved prompt template, no explanation."
        )
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60)
            ) as sess:
                async with sess.post(
                    f"{LM_URL}/v1/chat/completions",
                    json={
                        "model": base.model,
                        "messages": [{"role": "user", "content": improve_prompt}],
                        "max_tokens": 500,
                        "temperature": 0.5,
                    },
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        new_template = data["choices"][0]["message"]["content"].strip()
                        return self.add_variant(
                            name=f"{base.name} (v2)",
                            template=new_template,
                            system=base.system,
                            model=base.model,
                            temperature=base.temperature,
                            tags=base.tags + ["iterated"],
                            notes=f"Iterated from {base_variant_id}: {feedback[:100]}",
                        )
        except Exception as e:
            log.error(f"Iterate error: {e}")
        return base

    def score_run(self, run_id: str, score: float, notes: str = ""):
        for run in self._runs:
            if run.run_id == run_id:
                run.score = max(1.0, min(5.0, score))
                run.notes = notes
                break

    def _save_run(self, run: PlaygroundRun, variant: PromptVariant):
        runs_file = PLAYGROUND_DIR / f"runs_{variant.variant_id}.jsonl"
        with open(runs_file, "a") as f:
            f.write(json.dumps(run.to_dict()) + "\n")

    def variant_stats(self, variant_id: str) -> dict:
        runs = [r for r in self._runs if r.variant_id == variant_id]
        scored = [r for r in runs if r.score > 0]
        if not runs:
            return {"runs": 0}
        return {
            "variant_id": variant_id,
            "runs": len(runs),
            "avg_latency_ms": round(mean(r.latency_ms for r in runs), 1),
            "avg_tokens": round(mean(r.tokens_out for r in runs), 1),
            "scored_runs": len(scored),
            "avg_score": round(mean(r.score for r in scored), 2) if scored else None,
        }

    def list_variants(self) -> list[dict]:
        return [
            {
                **v.to_dict(),
                "runs": sum(1 for r in self._runs if r.variant_id == v.variant_id),
            }
            for v in sorted(self._variants.values(), key=lambda x: -x.created_at)
        ]


async def main():
    import sys

    pg = PromptPlayground()
    await pg.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        v1 = pg.add_variant(
            "code_explainer_v1",
            template="Explain this code in simple terms:\n\n{code}",
            tags=["code", "explain"],
        )
        v2 = pg.add_variant(
            "code_explainer_v2",
            template="You are a senior developer. Explain this code step-by-step for a junior developer:\n\n{code}",
            tags=["code", "explain"],
        )
        code = "async def main():\n    await asyncio.gather(*tasks)"
        runs = await pg.compare([v1.variant_id, v2.variant_id], {"code": code})
        for run in runs:
            v = pg._variants[run.variant_id]
            print(f"\n[{v.name}] {run.latency_ms:.0f}ms:")
            print(f"  {run.response[:150]}")

    elif cmd == "list":
        for v in pg.list_variants():
            print(
                f"  [{v['variant_id']}] {v['name']:<30} runs={v['runs']} tags={v['tags']}"
            )

    elif cmd == "run" and len(sys.argv) > 2:
        vid = sys.argv[2]
        vars_json = sys.argv[3] if len(sys.argv) > 3 else "{}"
        run = await pg.run(vid, json.loads(vars_json))
        print(f"[{run.run_id}] {run.latency_ms:.0f}ms | {run.tokens_out} tokens")
        print(run.response)


if __name__ == "__main__":
    asyncio.run(main())
