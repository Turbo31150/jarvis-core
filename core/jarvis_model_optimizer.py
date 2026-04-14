#!/usr/bin/env python3
"""
jarvis_model_optimizer — LLM inference parameter optimizer
Auto-tunes temperature, context length, batch size for perf/quality tradeoff
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.model_optimizer")

RESULTS_FILE = Path("/home/turbo/IA/Core/jarvis/data/model_optimizer_results.json")
REDIS_KEY = "jarvis:model_optimizer"

LM_URL = "http://127.0.0.1:1234"

BENCHMARK_PROMPTS = [
    ("speed", "Réponds en 1 mot: couleur du ciel?"),
    ("reasoning", "Si A>B et B>C, A>C est-il vrai? Réponds oui ou non."),
    ("code", "Python: fonction qui retourne le carré d'un nombre. Code seulement."),
    ("instruction", "Liste 3 avantages du GPU pour l'inférence LLM."),
]


@dataclass
class ParamConfig:
    temperature: float
    max_tokens: int
    top_p: float = 1.0
    repeat_penalty: float = 1.0

    def to_dict(self) -> dict:
        return {
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "top_p": self.top_p,
            "repeat_penalty": self.repeat_penalty,
        }


@dataclass
class OptimResult:
    model: str
    config: ParamConfig
    avg_tps: float = 0.0
    avg_latency_ms: float = 0.0
    quality_score: float = 0.0
    perf_score: float = 0.0  # combined score
    samples: int = 0
    ts: float = field(default_factory=time.time)

    @property
    def combined_score(self) -> float:
        """50% quality + 30% speed + 20% low latency."""
        speed_norm = min(1.0, self.avg_tps / 100)
        latency_norm = max(0.0, 1.0 - self.avg_latency_ms / 5000)
        return 0.5 * self.quality_score + 0.3 * speed_norm + 0.2 * latency_norm

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "config": self.config.to_dict(),
            "avg_tps": round(self.avg_tps, 1),
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "quality_score": round(self.quality_score, 3),
            "combined_score": round(self.combined_score, 3),
            "samples": self.samples,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.ts)),
        }


def _quality_heuristic(prompt_type: str, response: str) -> float:
    """Simple heuristic quality scoring."""
    if not response:
        return 0.0
    r = response.strip().lower()
    if prompt_type == "speed":
        # Short answer expected
        return 1.0 if len(response.split()) <= 3 else 0.7
    elif prompt_type == "reasoning":
        return 1.0 if any(w in r for w in ["oui", "vrai", "yes", "true"]) else 0.5
    elif prompt_type == "code":
        return 1.0 if "def " in r and "return" in r else 0.6
    elif prompt_type == "instruction":
        return min(1.0, r.count("\n") / 3 + 0.4)
    return 0.5


class ModelOptimizer:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._results: list[OptimResult] = []
        self._best: dict[str, OptimResult] = {}
        self._load()

    def _load(self):
        if RESULTS_FILE.exists():
            try:
                data = json.loads(RESULTS_FILE.read_text())
                for d in data.get("results", []):
                    cfg = ParamConfig(**d["config"])
                    r = OptimResult(
                        model=d["model"],
                        config=cfg,
                        avg_tps=d["avg_tps"],
                        avg_latency_ms=d["avg_latency_ms"],
                        quality_score=d["quality_score"],
                        samples=d["samples"],
                    )
                    self._results.append(r)
                # Rebuild best
                for r in self._results:
                    if (
                        r.model not in self._best
                        or r.combined_score > self._best[r.model].combined_score
                    ):
                        self._best[r.model] = r
            except Exception as e:
                log.warning(f"Load optimizer results error: {e}")

    def _save(self):
        RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        RESULTS_FILE.write_text(
            json.dumps(
                {"results": [r.to_dict() for r in self._results[-200:]]},
                indent=2,
            )
        )

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    async def _benchmark_config(
        self,
        model: str,
        config: ParamConfig,
        url: str = LM_URL,
    ) -> OptimResult:
        result = OptimResult(model=model, config=config)
        latencies = []
        tps_list = []
        quality_scores = []

        for prompt_type, prompt in BENCHMARK_PROMPTS:
            t0 = time.time()
            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=60)
                ) as sess:
                    async with sess.post(
                        f"{url}/v1/chat/completions",
                        json={
                            "model": model,
                            "messages": [{"role": "user", "content": prompt}],
                            **config.to_dict(),
                        },
                    ) as r:
                        data = await r.json()

                elapsed = time.time() - t0
                content = data["choices"][0]["message"]["content"]
                tokens = data.get("usage", {}).get("completion_tokens", 0)
                tps = tokens / elapsed if elapsed > 0 else 0

                latencies.append(elapsed * 1000)
                tps_list.append(tps)
                quality_scores.append(_quality_heuristic(prompt_type, content))

            except Exception as e:
                log.debug(f"Benchmark error [{prompt_type}]: {e}")
                latencies.append(5000)
                tps_list.append(0)
                quality_scores.append(0.0)

        result.avg_latency_ms = round(sum(latencies) / len(latencies), 1)
        result.avg_tps = round(sum(tps_list) / len(tps_list), 1)
        result.quality_score = round(sum(quality_scores) / len(quality_scores), 3)
        result.samples = len(BENCHMARK_PROMPTS)
        return result

    async def optimize(
        self,
        model: str,
        url: str = LM_URL,
        quick: bool = False,
    ) -> OptimResult:
        """Run parameter sweep and return best config."""
        log.info(f"Optimizing {model}...")

        # Parameter grid
        if quick:
            configs = [
                ParamConfig(temperature=0.0, max_tokens=512),
                ParamConfig(temperature=0.1, max_tokens=512),
                ParamConfig(temperature=0.0, max_tokens=256),
            ]
        else:
            configs = [
                ParamConfig(temperature=t, max_tokens=m, top_p=p)
                for t in [0.0, 0.1, 0.3]
                for m in [256, 512]
                for p in [1.0, 0.9]
            ]

        results = []
        for cfg in configs:
            log.info(
                f"  Testing temp={cfg.temperature} max_tokens={cfg.max_tokens} top_p={cfg.top_p}"
            )
            r = await self._benchmark_config(model, cfg, url)
            log.info(
                f"    → score={r.combined_score:.3f} tps={r.avg_tps:.1f} quality={r.quality_score:.2f}"
            )
            results.append(r)
            self._results.append(r)

        best = max(results, key=lambda r: r.combined_score)
        self._best[model] = best
        self._save()

        if self.redis:
            await self.redis.set(
                f"{REDIS_KEY}:{model}",
                json.dumps(best.to_dict()),
                ex=86400,
            )

        log.info(
            f"Best config for {model}: temp={best.config.temperature} max_tokens={best.config.max_tokens} score={best.combined_score:.3f}"
        )
        return best

    def get_best(self, model: str) -> OptimResult | None:
        return self._best.get(model)

    def recommendations(self) -> list[dict]:
        return [
            {
                "model": model,
                **result.to_dict(),
            }
            for model, result in self._best.items()
        ]


# ── CLI ───────────────────────────────────────────────────────────────────────


async def main():
    import sys

    opt = ModelOptimizer()
    await opt.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"

    if cmd == "optimize" and len(sys.argv) > 2:
        model = sys.argv[2]
        quick = "--quick" in sys.argv
        url = next(
            (sys.argv[i + 1] for i, a in enumerate(sys.argv) if a == "--url"), LM_URL
        )
        best = await opt.optimize(model, url=url, quick=quick)
        print(f"\n🏆 Best config for {model}:")
        print(json.dumps(best.to_dict(), indent=2))

    elif cmd == "list":
        recs = opt.recommendations()
        if not recs:
            print("No optimization results yet")
        else:
            print(
                f"{'Model':<40} {'Temp':>5} {'MaxTok':>7} {'TPS':>7} {'Quality':>8} {'Score':>7}"
            )
            print("-" * 80)
            for r in recs:
                print(
                    f"{r['model']:<40} {r['config']['temperature']:>5.1f} "
                    f"{r['config']['max_tokens']:>7} {r['avg_tps']:>7.1f} "
                    f"{r['quality_score']:>8.2f} {r['combined_score']:>7.3f}"
                )

    elif cmd == "best" and len(sys.argv) > 2:
        r = opt.get_best(sys.argv[2])
        if r:
            print(json.dumps(r.to_dict(), indent=2))
        else:
            print(f"No optimization results for '{sys.argv[2]}'")


if __name__ == "__main__":
    asyncio.run(main())
