#!/usr/bin/env python3
"""
jarvis_model_quantizer — Model quantization advisor and tracker
Recommends quantization configs, tracks VRAM savings, benchmarks quality tradeoffs
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.model_quantizer")

REDIS_PREFIX = "jarvis:quant:"
QUANT_DB = Path("/home/turbo/IA/Core/jarvis/data/quantization.json")

# VRAM savings vs quality retention (empirical estimates)
QUANT_PROFILES: dict[str, dict] = {
    "f32": {
        "vram_mult": 2.0,
        "quality": 1.00,
        "speed": 0.5,
        "desc": "Full precision (training)",
    },
    "f16": {
        "vram_mult": 1.0,
        "quality": 0.99,
        "speed": 1.0,
        "desc": "Half precision baseline",
    },
    "bf16": {
        "vram_mult": 1.0,
        "quality": 0.99,
        "speed": 1.0,
        "desc": "BFloat16 (better range)",
    },
    "q8_0": {
        "vram_mult": 0.53,
        "quality": 0.98,
        "speed": 1.1,
        "desc": "8-bit, near-lossless",
    },
    "q6_k": {"vram_mult": 0.41, "quality": 0.97, "speed": 1.2, "desc": "6-bit K-quant"},
    "q5_k_m": {
        "vram_mult": 0.34,
        "quality": 0.96,
        "speed": 1.3,
        "desc": "5-bit K-quant medium",
    },
    "q5_k_s": {
        "vram_mult": 0.33,
        "quality": 0.95,
        "speed": 1.3,
        "desc": "5-bit K-quant small",
    },
    "q4_k_m": {
        "vram_mult": 0.28,
        "quality": 0.94,
        "speed": 1.5,
        "desc": "4-bit K-quant medium (popular)",
    },
    "q4_k_s": {
        "vram_mult": 0.27,
        "quality": 0.92,
        "speed": 1.5,
        "desc": "4-bit K-quant small",
    },
    "q3_k_m": {
        "vram_mult": 0.22,
        "quality": 0.88,
        "speed": 1.7,
        "desc": "3-bit medium",
    },
    "q2_k": {"vram_mult": 0.16, "quality": 0.75, "speed": 2.0, "desc": "2-bit (lossy)"},
    "iq4_xs": {
        "vram_mult": 0.26,
        "quality": 0.93,
        "speed": 1.4,
        "desc": "iMatrix 4-bit XS",
    },
    "iq3_m": {
        "vram_mult": 0.21,
        "quality": 0.87,
        "speed": 1.6,
        "desc": "iMatrix 3-bit medium",
    },
}

# Base VRAM (fp16) for known models in MB
MODEL_BASE_VRAM: dict[str, int] = {
    "qwen3.5-9b": 5800,
    "qwen3.5-14b": 9200,
    "qwen3.5-27b": 16000,
    "qwen3.5-35b": 22000,
    "deepseek-r1-0528": 28000,
    "glm-4.7-flash": 5000,
    "gemma-4-26b": 16000,
    "llama3.2-3b": 2000,
    "llama3.1-8b": 5200,
}


@dataclass
class QuantRecommendation:
    model: str
    target_vram_mb: int
    recommended: str
    vram_mb: int
    quality: float
    speed_mult: float
    alternatives: list[dict] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "target_vram_mb": self.target_vram_mb,
            "recommended": self.recommended,
            "vram_mb": self.vram_mb,
            "quality": self.quality,
            "speed_mult": self.speed_mult,
            "reason": self.reason,
            "alternatives": self.alternatives,
        }


@dataclass
class QuantBenchmark:
    model: str
    quant: str
    vram_mb: int
    tok_per_s: float
    perplexity: float = 0.0
    mmlu_score: float = 0.0
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "quant": self.quant,
            "vram_mb": self.vram_mb,
            "tok_per_s": self.tok_per_s,
            "perplexity": self.perplexity,
            "mmlu_score": self.mmlu_score,
            "ts": self.ts,
        }


class ModelQuantizer:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._benchmarks: list[QuantBenchmark] = []
        self._load()

    def _load(self):
        if QUANT_DB.exists():
            try:
                data = json.loads(QUANT_DB.read_text())
                for d in data.get("benchmarks", []):
                    self._benchmarks.append(QuantBenchmark(**d))
            except Exception as e:
                log.warning(f"Quant DB load error: {e}")

    def _save(self):
        QUANT_DB.parent.mkdir(parents=True, exist_ok=True)
        QUANT_DB.write_text(
            json.dumps(
                {"benchmarks": [b.to_dict() for b in self._benchmarks]}, indent=2
            )
        )

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def _base_vram(self, model: str) -> int:
        for key, vram in MODEL_BASE_VRAM.items():
            if key.lower() in model.lower():
                return vram
        return 8000  # default guess

    def recommend(
        self,
        model: str,
        available_vram_mb: int,
        min_quality: float = 0.90,
        prefer_speed: bool = False,
    ) -> QuantRecommendation:
        base = self._base_vram(model)
        candidates = []

        for quant_name, profile in QUANT_PROFILES.items():
            vram = int(base * profile["vram_mult"])
            quality = profile["quality"]
            if vram <= available_vram_mb and quality >= min_quality:
                candidates.append(
                    {
                        "quant": quant_name,
                        "vram_mb": vram,
                        "quality": quality,
                        "speed_mult": profile["speed"],
                        "desc": profile["desc"],
                    }
                )

        if not candidates:
            # Relax quality constraint
            for quant_name, profile in QUANT_PROFILES.items():
                vram = int(base * profile["vram_mult"])
                if vram <= available_vram_mb:
                    candidates.append(
                        {
                            "quant": quant_name,
                            "vram_mb": vram,
                            "quality": profile["quality"],
                            "speed_mult": profile["speed"],
                            "desc": profile["desc"],
                        }
                    )

        if not candidates:
            # Model won't fit at all
            best_quant = min(QUANT_PROFILES.items(), key=lambda x: x[1]["vram_mult"])
            bname, bprofile = best_quant
            return QuantRecommendation(
                model=model,
                target_vram_mb=available_vram_mb,
                recommended=bname,
                vram_mb=int(base * bprofile["vram_mult"]),
                quality=bprofile["quality"],
                speed_mult=bprofile["speed"],
                reason=f"Model requires >{available_vram_mb}MB even at lowest quant",
            )

        # Sort by quality desc (or speed if preferred)
        sort_key = "speed_mult" if prefer_speed else "quality"
        candidates.sort(key=lambda c: c[sort_key], reverse=True)
        best = candidates[0]

        return QuantRecommendation(
            model=model,
            target_vram_mb=available_vram_mb,
            recommended=best["quant"],
            vram_mb=best["vram_mb"],
            quality=best["quality"],
            speed_mult=best["speed_mult"],
            alternatives=candidates[1:4],
            reason=f"Best {'speed' if prefer_speed else 'quality'} within {available_vram_mb}MB VRAM",
        )

    def compare(self, model: str, quants: list[str]) -> list[dict]:
        base = self._base_vram(model)
        results = []
        for q in quants:
            profile = QUANT_PROFILES.get(q)
            if not profile:
                continue
            results.append(
                {
                    "quant": q,
                    "vram_mb": int(base * profile["vram_mult"]),
                    "quality": profile["quality"],
                    "speed_mult": profile["speed"],
                    "desc": profile["desc"],
                    "vram_savings_pct": round((1 - profile["vram_mult"]) * 100, 1),
                }
            )
        return sorted(results, key=lambda x: x["quality"], reverse=True)

    def add_benchmark(
        self,
        model: str,
        quant: str,
        vram_mb: int,
        tok_per_s: float,
        perplexity: float = 0.0,
        mmlu_score: float = 0.0,
    ) -> QuantBenchmark:
        b = QuantBenchmark(
            model=model,
            quant=quant,
            vram_mb=vram_mb,
            tok_per_s=tok_per_s,
            perplexity=perplexity,
            mmlu_score=mmlu_score,
        )
        self._benchmarks.append(b)
        self._save()
        return b

    def get_benchmarks(self, model: str) -> list[QuantBenchmark]:
        return [b for b in self._benchmarks if model.lower() in b.model.lower()]

    def stats(self) -> dict:
        return {
            "benchmarks": len(self._benchmarks),
            "models": len({b.model for b in self._benchmarks}),
            "quant_types": len(QUANT_PROFILES),
        }


async def main():
    import sys

    qtz = ModelQuantizer()
    await qtz.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        models = [
            ("qwen3.5-9b", 6144),
            ("qwen3.5-27b", 10240),
            ("deepseek-r1-0528", 12288),
        ]
        print(
            f"{'Model':<20} {'VRAM avail':>10} {'Recommended':<12} {'VRAM':>8} {'Quality':>8}"
        )
        print("-" * 65)
        for model, avail in models:
            rec = qtz.recommend(model, avail)
            print(
                f"  {model:<20} {avail:>10} {rec.recommended:<12} {rec.vram_mb:>7}MB {rec.quality:>7.0%}"
            )
            for alt in rec.alternatives[:2]:
                print(
                    f"    alt: {alt['quant']:<10} {alt['vram_mb']:>7}MB {alt['quality']:>7.0%}"
                )

    elif cmd == "compare" and len(sys.argv) > 2:
        model = sys.argv[2]
        quants = sys.argv[3:] if len(sys.argv) > 3 else list(QUANT_PROFILES.keys())
        results = qtz.compare(model, quants)
        print(f"\n{'Quant':<12} {'VRAM':>8} {'Quality':>8} {'Speed':>6} {'Savings':>8}")
        print("-" * 50)
        for r in results:
            print(
                f"  {r['quant']:<12} {r['vram_mb']:>7}MB {r['quality']:>7.0%} {r['speed_mult']:>5.1f}x {r['vram_savings_pct']:>7.1f}%"
            )

    elif cmd == "stats":
        print(json.dumps(qtz.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
