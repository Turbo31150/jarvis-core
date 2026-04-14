#!/usr/bin/env python3
"""
jarvis_ab_test_engine — A/B testing framework for prompts, models, parameters
Run controlled experiments, measure quality metrics, select winners
"""

import asyncio
import json
import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean, stdev

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.ab_test")

EXPERIMENTS_FILE = Path("/home/turbo/IA/Core/jarvis/data/ab_experiments.json")
REDIS_PREFIX = "jarvis:abtest:"


@dataclass
class Variant:
    name: str
    model: str
    backend_url: str
    prompt_template: str | None = None
    temperature: float = 0.0
    max_tokens: int = 512
    weight: float = 1.0  # relative selection weight
    # Metrics
    requests: int = 0
    successes: int = 0
    latencies: list[float] = field(default_factory=list)
    scores: list[float] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        return self.successes / self.requests if self.requests > 0 else 0.0

    @property
    def avg_latency_ms(self) -> float:
        return mean(self.latencies) * 1000 if self.latencies else 0.0

    @property
    def avg_score(self) -> float:
        return mean(self.scores) if self.scores else 0.0

    @property
    def score_stdev(self) -> float:
        return stdev(self.scores) if len(self.scores) >= 2 else 0.0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "model": self.model,
            "backend_url": self.backend_url,
            "prompt_template": self.prompt_template,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "weight": self.weight,
            "requests": self.requests,
            "successes": self.successes,
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "avg_score": round(self.avg_score, 3),
            "score_stdev": round(self.score_stdev, 3),
            "success_rate": round(self.success_rate, 3),
        }


@dataclass
class Experiment:
    exp_id: str
    name: str
    description: str = ""
    variants: list[Variant] = field(default_factory=list)
    status: str = "running"  # running | stopped | concluded
    winner: str | None = None
    created_at: float = field(default_factory=time.time)
    min_samples: int = 20  # samples per variant before concluding

    def select_variant(self) -> Variant:
        """Weighted random selection."""
        weights = [v.weight for v in self.variants]
        return random.choices(self.variants, weights=weights, k=1)[0]

    def conclude(self) -> str | None:
        """Select winner by highest avg_score, minimum samples check."""
        ready = [v for v in self.variants if v.requests >= self.min_samples]
        if len(ready) < len(self.variants):
            return None
        winner = max(ready, key=lambda v: v.avg_score)
        self.winner = winner.name
        self.status = "concluded"
        return winner.name

    def to_dict(self) -> dict:
        return {
            "exp_id": self.exp_id,
            "name": self.name,
            "description": self.description,
            "status": self.status,
            "winner": self.winner,
            "created_at": self.created_at,
            "min_samples": self.min_samples,
            "variants": [v.to_dict() for v in self.variants],
        }


class ABTestEngine:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._experiments: dict[str, Experiment] = {}
        self._load()

    def _load(self):
        if EXPERIMENTS_FILE.exists():
            try:
                data = json.loads(EXPERIMENTS_FILE.read_text())
                for d in data.get("experiments", []):
                    exp = Experiment(
                        exp_id=d["exp_id"],
                        name=d["name"],
                        description=d.get("description", ""),
                        status=d.get("status", "running"),
                        winner=d.get("winner"),
                        created_at=d.get("created_at", time.time()),
                        min_samples=d.get("min_samples", 20),
                    )
                    for vd in d.get("variants", []):
                        v = Variant(
                            name=vd["name"],
                            model=vd["model"],
                            backend_url=vd["backend_url"],
                            prompt_template=vd.get("prompt_template"),
                            temperature=vd.get("temperature", 0.0),
                            max_tokens=vd.get("max_tokens", 512),
                            weight=vd.get("weight", 1.0),
                            requests=vd.get("requests", 0),
                            successes=vd.get("successes", 0),
                        )
                        exp.variants.append(v)
                    self._experiments[exp.exp_id] = exp
            except Exception as e:
                log.warning(f"Load experiments error: {e}")

    def _save(self):
        EXPERIMENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        EXPERIMENTS_FILE.write_text(
            json.dumps(
                {"experiments": [e.to_dict() for e in self._experiments.values()]},
                indent=2,
            )
        )

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def create_experiment(
        self,
        name: str,
        variants: list[dict],
        description: str = "",
        min_samples: int = 20,
    ) -> Experiment:
        exp_id = str(uuid.uuid4())[:8]
        exp = Experiment(
            exp_id=exp_id,
            name=name,
            description=description,
            min_samples=min_samples,
        )
        for vd in variants:
            exp.variants.append(Variant(**vd))
        self._experiments[exp_id] = exp
        self._save()
        log.info(f"Experiment created: {name} [{exp_id}] with {len(variants)} variants")
        return exp

    async def run_trial(
        self,
        exp_id: str,
        prompt: str,
        scorer: callable | None = None,
    ) -> dict:
        exp = self._experiments.get(exp_id)
        if not exp or exp.status != "running":
            return {"error": f"Experiment {exp_id} not found or not running"}

        variant = exp.select_variant()
        t0 = time.time()

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=120)
            ) as sess:
                payload = {
                    "model": variant.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": variant.max_tokens,
                    "temperature": variant.temperature,
                }
                async with sess.post(
                    f"{variant.backend_url}/v1/chat/completions", json=payload
                ) as r:
                    data = await r.json()
                    ok = r.status == 200

            latency = time.time() - t0
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")

            # Score
            score = 0.5  # default
            if scorer and content:
                try:
                    score = float(scorer(content, prompt))
                except Exception:
                    pass
            elif content:
                # Default: non-empty response = 1.0, quality proxy = len/200 capped at 1
                score = min(1.0, len(content) / 200)

            variant.requests += 1
            if ok:
                variant.successes += 1
            variant.latencies.append(latency)
            variant.scores.append(score)

            self._save()

            # Check if can conclude
            winner = exp.conclude()
            if winner:
                log.info(f"Experiment {exp.name} concluded: winner = {winner}")

            return {
                "exp_id": exp_id,
                "variant": variant.name,
                "model": variant.model,
                "latency_s": round(latency, 3),
                "score": round(score, 3),
                "content": content[:200],
                "concluded": exp.status == "concluded",
                "winner": exp.winner,
            }

        except Exception as e:
            variant.requests += 1
            variant.scores.append(0.0)
            self._save()
            return {"error": str(e), "variant": variant.name}

    def get_results(self, exp_id: str) -> dict | None:
        exp = self._experiments.get(exp_id)
        if not exp:
            return None
        return exp.to_dict()

    def list_experiments(self) -> list[dict]:
        return [
            {
                "exp_id": e.exp_id,
                "name": e.name,
                "status": e.status,
                "winner": e.winner,
                "total_requests": sum(v.requests for v in e.variants),
                "variants": len(e.variants),
            }
            for e in self._experiments.values()
        ]


# ── CLI ───────────────────────────────────────────────────────────────────────


async def main():
    import sys

    engine = ABTestEngine()
    await engine.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"

    if cmd == "list":
        experiments = engine.list_experiments()
        if not experiments:
            print("No experiments")
        else:
            print(f"{'ID':<10} {'Name':<24} {'Status':<12} {'Requests':>9} {'Winner'}")
            print("-" * 65)
            for e in experiments:
                print(
                    f"{e['exp_id']:<10} {e['name']:<24} {e['status']:<12} "
                    f"{e['total_requests']:>9} {e['winner'] or '-'}"
                )

    elif cmd == "results" and len(sys.argv) > 2:
        r = engine.get_results(sys.argv[2])
        if r:
            print(json.dumps(r, indent=2))
        else:
            print(f"Experiment '{sys.argv[2]}' not found")

    elif cmd == "demo":
        exp = engine.create_experiment(
            name="model_comparison",
            description="Compare qwen3.5-9b vs qwen3-8b",
            min_samples=5,
            variants=[
                {
                    "name": "qwen9b",
                    "model": "qwen/qwen3.5-9b",
                    "backend_url": "http://127.0.0.1:1234",
                },
                {
                    "name": "qwen8b",
                    "model": "qwen/qwen3-8b",
                    "backend_url": "http://127.0.0.1:1234",
                },
            ],
        )
        print(f"Experiment: {exp.exp_id}")
        prompts = [
            "Explique CUDA",
            "C'est quoi Redis?",
            "Python vs Go",
            "GPU inference optimisation",
            "LLM quantization",
        ]
        for p in prompts * 2:
            r = await engine.run_trial(exp.exp_id, p)
            print(
                f"  [{r.get('variant')}] score={r.get('score', 0):.2f} lat={r.get('latency_s', 0):.2f}s"
            )
            if r.get("concluded"):
                print(f"\n🏆 Winner: {r['winner']}")
                break


if __name__ == "__main__":
    asyncio.run(main())
