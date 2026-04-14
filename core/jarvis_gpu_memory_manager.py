#!/usr/bin/env python3
"""
jarvis_gpu_memory_manager — GPU VRAM allocation tracking and optimization
Tracks model VRAM usage, prevents OOM, suggests optimal model placement across GPUs
"""

import asyncio
import json
import logging
import subprocess
import time
from dataclasses import dataclass, field

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.gpu_memory_manager")

REDIS_KEY = "jarvis:gpu_memory"

# Known VRAM footprints in MB (approximate at Q4_K_M)
MODEL_VRAM_MB: dict[str, int] = {
    "qwen3.5-9b": 5800,
    "qwen3.5-27b-claude": 16000,
    "qwen3.5-35b-a3b": 22000,
    "qwen3-14b": 9000,
    "deepseek-r1-0528": 28000,
    "glm-4.7-flash": 5000,
    "gemma-4-26b": 17000,
    "gpt-oss-20b": 13000,
    "nomic-embed": 600,
}

# Physical GPU VRAM in MB (detected from nvidia-smi)
GPU_VRAM_MB: dict[int, int] = {
    0: 6144,  # RTX 2060 (GPU0 in LMS = GPU1 in system)
    1: 6144,  # RTX 1660S
    2: 6144,  # RTX 1660S
    3: 6144,  # RTX 1660S
    4: 10240,  # RTX 3080
}


@dataclass
class GPUState:
    idx: int
    name: str
    vram_total_mb: int
    vram_used_mb: int
    vram_free_mb: int
    temp_c: int
    util_pct: int
    loaded_models: list[str] = field(default_factory=list)

    @property
    def vram_pct(self) -> float:
        return round(self.vram_used_mb / max(self.vram_total_mb, 1) * 100, 1)

    @property
    def can_fit(self) -> int:
        """Free VRAM with 10% safety margin."""
        return int(self.vram_free_mb * 0.90)

    def to_dict(self) -> dict:
        return {
            "idx": self.idx,
            "name": self.name,
            "vram_total_mb": self.vram_total_mb,
            "vram_used_mb": self.vram_used_mb,
            "vram_free_mb": self.vram_free_mb,
            "vram_pct": self.vram_pct,
            "temp_c": self.temp_c,
            "util_pct": self.util_pct,
            "loaded_models": self.loaded_models,
        }


@dataclass
class PlacementPlan:
    model: str
    required_mb: int
    recommended_gpus: list[int]
    strategy: str  # single | split | offload
    feasible: bool
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "required_mb": self.required_mb,
            "recommended_gpus": self.recommended_gpus,
            "strategy": self.strategy,
            "feasible": self.feasible,
            "notes": self.notes,
        }


class GPUMemoryManager:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._gpus: dict[int, GPUState] = {}
        self._allocations: dict[str, dict] = {}  # model → {gpus, mb}

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def refresh(self) -> list[GPUState]:
        """Query nvidia-smi for current GPU state."""
        gpus = []
        try:
            r = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=index,name,memory.total,memory.used,memory.free,temperature.gpu,utilization.gpu",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            for line in r.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                p = [x.strip() for x in line.split(",")]
                if len(p) < 7:
                    continue
                gpu = GPUState(
                    idx=int(p[0]),
                    name=p[1],
                    vram_total_mb=int(float(p[2])),
                    vram_used_mb=int(float(p[3])),
                    vram_free_mb=int(float(p[4])),
                    temp_c=int(float(p[5])),
                    util_pct=int(float(p[6])),
                )
                self._gpus[gpu.idx] = gpu
                gpus.append(gpu)
        except Exception as e:
            log.warning(f"nvidia-smi error: {e}")
        return gpus

    def plan_placement(
        self, model: str, vram_override_mb: int | None = None
    ) -> PlacementPlan:
        """Suggest optimal GPU placement for a model."""
        required_mb = vram_override_mb or self._estimate_vram(model)
        self.refresh()

        if not self._gpus:
            return PlacementPlan(
                model=model,
                required_mb=required_mb,
                recommended_gpus=[],
                strategy="unknown",
                feasible=False,
                notes="No GPU data available",
            )

        # Sort GPUs by free VRAM descending
        sorted_gpus = sorted(self._gpus.values(), key=lambda g: -g.vram_free_mb)

        # Strategy 1: single GPU fit
        single_fits = [g for g in sorted_gpus if g.can_fit >= required_mb]
        if single_fits:
            best = single_fits[0]
            return PlacementPlan(
                model=model,
                required_mb=required_mb,
                recommended_gpus=[best.idx],
                strategy="single",
                feasible=True,
                notes=f"GPU{best.idx} ({best.name}): {best.vram_free_mb}MB free → fits with {best.vram_free_mb - required_mb}MB spare",
            )

        # Strategy 2: split across multiple GPUs
        total_free = sum(g.vram_free_mb for g in self._gpus.values())
        if total_free >= required_mb:
            # Use top N GPUs with most free memory
            split_gpus = []
            accumulated = 0
            for g in sorted_gpus:
                split_gpus.append(g.idx)
                accumulated += g.vram_free_mb
                if accumulated >= required_mb:
                    break
            return PlacementPlan(
                model=model,
                required_mb=required_mb,
                recommended_gpus=split_gpus,
                strategy="split",
                feasible=True,
                notes=f"Tensor split across GPUs {split_gpus} ({accumulated}MB combined)",
            )

        # Strategy 3: offload to CPU RAM
        return PlacementPlan(
            model=model,
            required_mb=required_mb,
            recommended_gpus=[g.idx for g in sorted_gpus[:2]],
            strategy="offload",
            feasible=True,
            notes=f"Insufficient VRAM ({total_free}MB), partial CPU offload needed",
        )

    def _estimate_vram(self, model: str) -> int:
        """Estimate VRAM requirement from known models or name heuristics."""
        # Exact match
        for known, mb in MODEL_VRAM_MB.items():
            if known.lower() in model.lower():
                return mb
        # Heuristic from name
        name_lower = model.lower()
        if "70b" in name_lower:
            return 42000
        if "35b" in name_lower or "34b" in name_lower:
            return 22000
        if "27b" in name_lower:
            return 16000
        if "14b" in name_lower or "13b" in name_lower:
            return 9000
        if "9b" in name_lower or "8b" in name_lower or "7b" in name_lower:
            return 5800
        if "3b" in name_lower or "4b" in name_lower:
            return 2500
        if "1b" in name_lower:
            return 1000
        return 8000  # default: unknown model

    def can_load(self, model: str) -> bool:
        plan = self.plan_placement(model)
        return plan.feasible and plan.strategy in ("single", "split")

    def register_allocation(self, model: str, gpu_ids: list[int], mb: int):
        self._allocations[model] = {"gpus": gpu_ids, "mb": mb, "ts": time.time()}
        for gpu_id in gpu_ids:
            if gpu_id in self._gpus:
                if model not in self._gpus[gpu_id].loaded_models:
                    self._gpus[gpu_id].loaded_models.append(model)

    def free_allocation(self, model: str):
        alloc = self._allocations.pop(model, None)
        if alloc:
            for gpu_id in alloc["gpus"]:
                if gpu_id in self._gpus:
                    self._gpus[gpu_id].loaded_models = [
                        m for m in self._gpus[gpu_id].loaded_models if m != model
                    ]

    def total_allocated_mb(self) -> int:
        return sum(a["mb"] for a in self._allocations.values())

    async def sync_redis(self):
        if not self.redis:
            return
        state = {
            "ts": time.time(),
            "gpus": {str(idx): g.to_dict() for idx, g in self._gpus.items()},
            "allocations": self._allocations,
        }
        await self.redis.set(REDIS_KEY, json.dumps(state), ex=60)

    def summary(self) -> dict:
        self.refresh()
        total_vram = sum(g.vram_total_mb for g in self._gpus.values())
        used_vram = sum(g.vram_used_mb for g in self._gpus.values())
        return {
            "gpus": len(self._gpus),
            "total_vram_mb": total_vram,
            "used_vram_mb": used_vram,
            "free_vram_mb": total_vram - used_vram,
            "utilization_pct": round(used_vram / max(total_vram, 1) * 100, 1),
            "allocations": len(self._allocations),
            "allocated_mb": self.total_allocated_mb(),
        }


async def main():
    import sys

    mgr = GPUMemoryManager()
    await mgr.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "status":
        gpus = mgr.refresh()
        if not gpus:
            print("No GPU data (nvidia-smi unavailable)")
        else:
            print(
                f"{'GPU':<4} {'Name':<22} {'Used':>8} {'Free':>8} {'Total':>8} {'%':>5} {'Temp':>6}"
            )
            print("-" * 65)
            for g in gpus:
                print(
                    f"  {g.idx:<4} {g.name:<22} {g.vram_used_mb:>6}MB {g.vram_free_mb:>6}MB {g.vram_total_mb:>6}MB {g.vram_pct:>4.0f}% {g.temp_c:>4}°C"
                )
            s = mgr.summary()
            print(
                f"\nTotal: {s['used_vram_mb']}/{s['total_vram_mb']}MB ({s['utilization_pct']}%)"
            )

    elif cmd == "plan" and len(sys.argv) > 2:
        model = " ".join(sys.argv[2:])
        plan = mgr.plan_placement(model)
        print(f"Model: {model}")
        print(f"Required: {plan.required_mb}MB")
        print(
            f"Strategy: {plan.strategy} | Feasible: {'✅' if plan.feasible else '❌'}"
        )
        print(f"GPUs: {plan.recommended_gpus}")
        print(f"Notes: {plan.notes}")

    elif cmd == "can-load" and len(sys.argv) > 2:
        model = " ".join(sys.argv[2:])
        ok = mgr.can_load(model)
        print(f"{'✅' if ok else '❌'} Can load '{model}': {ok}")


if __name__ == "__main__":
    asyncio.run(main())
