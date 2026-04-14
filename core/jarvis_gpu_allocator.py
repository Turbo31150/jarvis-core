#!/usr/bin/env python3
"""
jarvis_gpu_allocator — GPU VRAM allocation tracking and placement optimizer
Tracks model VRAM usage per GPU, prevents OOM, suggests optimal placement
"""

import asyncio
import json
import logging
import subprocess
import time
from dataclasses import dataclass, field

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.gpu_allocator")

REDIS_PREFIX = "jarvis:gpu_alloc:"

# Known model VRAM requirements in MB
MODEL_VRAM_MB: dict[str, int] = {
    "qwen3.5-9b": 5800,
    "qwen3.5-27b-claude": 15000,
    "qwen3.5-35b-a3b": 20000,
    "deepseek-r1-0528": 32000,
    "glm-4.7-flash-claude": 4200,
    "nomic-embed-text": 300,
    "gemma3:4b": 2800,
    "llama3.2": 2100,
    "gpt-oss-20b": 12000,
    "default": 4096,
}

THERMAL_LIMIT_C = 85


@dataclass
class GPUInfo:
    gpu_id: int
    name: str
    total_mb: int
    used_mb: int
    free_mb: int
    temp_c: float
    util_pct: float
    loaded_models: list[str] = field(default_factory=list)

    @property
    def available_mb(self) -> int:
        # Leave 512MB headroom
        return max(0, self.free_mb - 512)

    @property
    def hot(self) -> bool:
        return self.temp_c >= THERMAL_LIMIT_C

    def to_dict(self) -> dict:
        return {
            "gpu_id": self.gpu_id,
            "name": self.name,
            "total_mb": self.total_mb,
            "used_mb": self.used_mb,
            "free_mb": self.free_mb,
            "available_mb": self.available_mb,
            "temp_c": self.temp_c,
            "util_pct": self.util_pct,
            "hot": self.hot,
            "loaded_models": self.loaded_models,
        }


@dataclass
class AllocationResult:
    model: str
    gpu_id: int
    vram_mb: int
    success: bool
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "gpu_id": self.gpu_id,
            "vram_mb": self.vram_mb,
            "success": self.success,
            "reason": self.reason,
        }


class GPUAllocator:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._gpus: dict[int, GPUInfo] = {}
        self._allocations: dict[str, int] = {}  # model → gpu_id
        self._history: list[dict] = []
        self._last_scan: float = 0.0

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def _parse_nvidia_smi(self) -> list[GPUInfo]:
        try:
            out = subprocess.check_output(
                [
                    "nvidia-smi",
                    "--query-gpu=index,name,memory.total,memory.used,memory.free,temperature.gpu,utilization.gpu",
                    "--format=csv,noheader,nounits",
                ],
                timeout=5,
                stderr=subprocess.DEVNULL,
            ).decode()
            gpus = []
            for line in out.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 7:
                    continue
                gpu_id = int(parts[0])
                gpus.append(
                    GPUInfo(
                        gpu_id=gpu_id,
                        name=parts[1],
                        total_mb=int(parts[2]),
                        used_mb=int(parts[3]),
                        free_mb=int(parts[4]),
                        temp_c=float(parts[5]),
                        util_pct=float(parts[6]),
                        loaded_models=[
                            m for m, gid in self._allocations.items() if gid == gpu_id
                        ],
                    )
                )
            return gpus
        except Exception as e:
            log.debug(f"nvidia-smi error: {e}")
            return []

    async def scan(self) -> list[GPUInfo]:
        gpus = self._parse_nvidia_smi()
        if not gpus:
            # Synthetic fallback for testing
            gpus = [
                GPUInfo(0, "RTX 3090", 24576, 8192, 16384, 62.0, 45.0),
                GPUInfo(1, "RTX 3060", 6144, 1024, 5120, 54.0, 20.0),
            ]

        for gpu in gpus:
            self._gpus[gpu.gpu_id] = gpu

        self._last_scan = time.time()

        if self.redis:
            for gpu in gpus:
                await self.redis.setex(
                    f"{REDIS_PREFIX}gpu:{gpu.gpu_id}",
                    60,
                    json.dumps(gpu.to_dict()),
                )

        return gpus

    def _get_model_vram(self, model: str) -> int:
        for key, vram in MODEL_VRAM_MB.items():
            if key.lower() in model.lower():
                return vram
        return MODEL_VRAM_MB["default"]

    def allocate(
        self,
        model: str,
        preferred_gpu: int | None = None,
        require_free: bool = True,
    ) -> AllocationResult:
        vram_needed = self._get_model_vram(model)

        # Already allocated
        if model in self._allocations:
            return AllocationResult(
                model=model,
                gpu_id=self._allocations[model],
                vram_mb=vram_needed,
                success=True,
                reason="already_allocated",
            )

        candidates = list(self._gpus.values())
        if not candidates:
            return AllocationResult(
                model=model,
                gpu_id=-1,
                vram_mb=vram_needed,
                success=False,
                reason="no_gpus_scanned",
            )

        # Filter: not hot, enough VRAM
        valid = [g for g in candidates if not g.hot and g.available_mb >= vram_needed]

        if not valid:
            if require_free:
                return AllocationResult(
                    model=model,
                    gpu_id=-1,
                    vram_mb=vram_needed,
                    success=False,
                    reason=f"no_gpu_with_{vram_needed}MB_free",
                )
            valid = [g for g in candidates if not g.hot]
            if not valid:
                return AllocationResult(
                    model=model,
                    gpu_id=-1,
                    vram_mb=vram_needed,
                    success=False,
                    reason="all_gpus_hot",
                )

        # Prefer requested GPU
        if preferred_gpu is not None:
            pref = next((g for g in valid if g.gpu_id == preferred_gpu), None)
            if pref:
                chosen = pref
            else:
                chosen = max(valid, key=lambda g: g.available_mb)
        else:
            # Best-fit: most free VRAM
            chosen = max(valid, key=lambda g: g.available_mb)

        self._allocations[model] = chosen.gpu_id
        chosen.loaded_models.append(model)
        chosen.used_mb += vram_needed
        chosen.free_mb -= vram_needed

        result = AllocationResult(
            model=model, gpu_id=chosen.gpu_id, vram_mb=vram_needed, success=True
        )
        self._history.append(result.to_dict())
        log.info(f"Allocated {model} → GPU{chosen.gpu_id} ({vram_needed}MB)")
        return result

    def deallocate(self, model: str) -> bool:
        gpu_id = self._allocations.pop(model, None)
        if gpu_id is None:
            return False
        gpu = self._gpus.get(gpu_id)
        if gpu:
            vram = self._get_model_vram(model)
            gpu.used_mb = max(0, gpu.used_mb - vram)
            gpu.free_mb = min(gpu.total_mb, gpu.free_mb + vram)
            if model in gpu.loaded_models:
                gpu.loaded_models.remove(model)
        log.info(f"Deallocated {model} from GPU{gpu_id}")
        return True

    def placement_report(self) -> list[dict]:
        return [g.to_dict() for g in self._gpus.values()]

    def stats(self) -> dict:
        total_vram = sum(g.total_mb for g in self._gpus.values())
        used_vram = sum(g.used_mb for g in self._gpus.values())
        return {
            "gpus": len(self._gpus),
            "allocated_models": len(self._allocations),
            "total_vram_mb": total_vram,
            "used_vram_mb": used_vram,
            "free_vram_mb": total_vram - used_vram,
            "utilization_pct": round(used_vram / max(total_vram, 1) * 100, 1),
            "hot_gpus": sum(1 for g in self._gpus.values() if g.hot),
        }


async def main():
    import sys

    allocator = GPUAllocator()
    await allocator.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        await allocator.scan()
        print("GPU Status:")
        for gpu in allocator.placement_report():
            print(
                f"  GPU{gpu['gpu_id']} {gpu['name']}: {gpu['free_mb']}MB free, {gpu['temp_c']}°C"
            )

        models = ["qwen3.5-9b", "nomic-embed-text", "glm-4.7-flash-claude"]
        print("\nAllocations:")
        for model in models:
            result = allocator.allocate(model)
            status = "✅" if result.success else "❌"
            print(
                f"  {status} {model} → GPU{result.gpu_id} ({result.vram_mb}MB) {result.reason}"
            )

        print(f"\nStats: {json.dumps(allocator.stats(), indent=2)}")

    elif cmd == "scan":
        gpus = await allocator.scan()
        for gpu in gpus:
            print(json.dumps(gpu.to_dict(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
