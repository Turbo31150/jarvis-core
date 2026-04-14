#!/usr/bin/env python3
"""JARVIS GPU Scheduler — Schedule model loads across GPUs with VRAM tracking"""

import redis
import json
import time

r = redis.Redis(decode_responses=True)

GPU_PREFIX = "jarvis:gpusched:gpu:"
MODEL_PREFIX = "jarvis:gpusched:model:"
QUEUE_KEY = "jarvis:gpusched:queue"
STATS_KEY = "jarvis:gpusched:stats"

# GPU inventory (updated from telemetry)
GPU_INVENTORY = {
    0: {"node": "m1", "vram_total_mb": 12288},
    1: {"node": "m1", "vram_total_mb": 6144},
    2: {"node": "m1", "vram_total_mb": 6144},
    3: {"node": "m1", "vram_total_mb": 6144},
    4: {"node": "m1", "vram_total_mb": 10240},
}

MODEL_SIZES = {
    "qwen3.5-35b-a3b": {"vram_mb": 22000, "priority": 9},
    "qwen3.5-9b": {"vram_mb": 6000, "priority": 7},
    "qwen3-8b": {"vram_mb": 5500, "priority": 7},
    "mistral-7b-instruct-v0.3": {"vram_mb": 5000, "priority": 6},
    "deepseek-r1-0528": {"vram_mb": 8000, "priority": 8},
    "gemma3:4b": {"vram_mb": 3000, "priority": 5},
    "phi-3.1-mini-128k": {"vram_mb": 2500, "priority": 5},
}


def _get_gpu_state(gpu_id: int) -> dict:
    raw = r.get(f"{GPU_PREFIX}{gpu_id}")
    if raw:
        return json.loads(raw)
    return {
        "id": gpu_id,
        "vram_used_mb": 0,
        "vram_total_mb": GPU_INVENTORY.get(gpu_id, {}).get("vram_total_mb", 0),
        "loaded_models": [],
        "temp_c": 35,
    }


def _save_gpu_state(state: dict):
    r.setex(f"{GPU_PREFIX}{state['id']}", 3600, json.dumps(state))


def update_from_telemetry(gpu_id: int, vram_used_mb: float, temp_c: float):
    state = _get_gpu_state(gpu_id)
    state["vram_used_mb"] = vram_used_mb
    state["temp_c"] = temp_c
    state["last_updated"] = time.time()
    _save_gpu_state(state)


def load_model(model_name: str) -> dict:
    """Find best GPU for model and schedule load."""
    model_info = MODEL_SIZES.get(model_name)
    if not model_info:
        return {"ok": False, "error": f"Unknown model: {model_name}"}

    vram_needed = model_info["vram_mb"]
    best_gpu = None
    best_free = 0

    for gpu_id, cfg in GPU_INVENTORY.items():
        state = _get_gpu_state(gpu_id)
        # Skip hot GPUs
        if state.get("temp_c", 0) > 85:
            continue
        free = state["vram_total_mb"] - state["vram_used_mb"]
        if free >= vram_needed and free > best_free:
            best_free = free
            best_gpu = gpu_id

    if best_gpu is None:
        r.hincrby(STATS_KEY, "oom_events", 1)
        return {
            "ok": False,
            "error": "No GPU with sufficient VRAM",
            "needed_mb": vram_needed,
        }

    state = _get_gpu_state(best_gpu)
    state["vram_used_mb"] += vram_needed
    if model_name not in state["loaded_models"]:
        state["loaded_models"].append(model_name)
    _save_gpu_state(state)

    r.setex(
        f"{MODEL_PREFIX}{model_name}",
        3600,
        json.dumps(
            {
                "model": model_name,
                "gpu_id": best_gpu,
                "vram_mb": vram_needed,
                "loaded_at": time.time(),
            }
        ),
    )
    r.hincrby(STATS_KEY, "models_loaded", 1)
    return {
        "ok": True,
        "gpu_id": best_gpu,
        "vram_used_mb": state["vram_used_mb"],
        "vram_free_mb": state["vram_total_mb"] - state["vram_used_mb"],
    }


def unload_model(model_name: str) -> bool:
    raw = r.get(f"{MODEL_PREFIX}{model_name}")
    if not raw:
        return False
    info = json.loads(raw)
    gpu_id = info["gpu_id"]
    state = _get_gpu_state(gpu_id)
    state["vram_used_mb"] = max(0, state["vram_used_mb"] - info["vram_mb"])
    if model_name in state["loaded_models"]:
        state["loaded_models"].remove(model_name)
    _save_gpu_state(state)
    r.delete(f"{MODEL_PREFIX}{model_name}")
    r.hincrby(STATS_KEY, "models_unloaded", 1)
    return True


def get_model_placement(model_name: str) -> dict | None:
    raw = r.get(f"{MODEL_PREFIX}{model_name}")
    return json.loads(raw) if raw else None


def cluster_vram_report() -> list:
    rows = []
    for gpu_id in GPU_INVENTORY:
        state = _get_gpu_state(gpu_id)
        total = state["vram_total_mb"]
        used = state["vram_used_mb"]
        rows.append(
            {
                "gpu": gpu_id,
                "used_mb": used,
                "total_mb": total,
                "free_mb": total - used,
                "pct": round(used / max(total, 1) * 100, 1),
                "models": state["loaded_models"],
                "temp_c": state.get("temp_c", 0),
            }
        )
    return rows


def stats() -> dict:
    s = r.hgetall(STATS_KEY)
    return {k: int(v) for k, v in s.items()}


if __name__ == "__main__":
    # Seed telemetry
    update_from_telemetry(0, 515, 35)
    update_from_telemetry(1, 10, 32)
    update_from_telemetry(2, 10, 33)
    update_from_telemetry(3, 10, 30)
    update_from_telemetry(4, 75, 32)

    print("Loading models:")
    models_to_load = [
        "qwen3.5-35b-a3b",
        "deepseek-r1-0528",
        "mistral-7b-instruct-v0.3",
        "gemma3:4b",
        "phi-3.1-mini-128k",
        "qwen3.5-9b",
    ]
    for model in models_to_load:
        result = load_model(model)
        if result["ok"]:
            print(
                f"  ✅ {model:30s} → GPU{result['gpu_id']}  "
                f"free={result['vram_free_mb']}MB"
            )
        else:
            print(f"  ❌ {model:30s}  {result['error']}")

    print("\nVRAM Report:")
    for row in cluster_vram_report():
        bar = "█" * int(row["pct"] / 5) + "░" * (20 - int(row["pct"] / 5))
        print(
            f"  GPU{row['gpu']} {bar} {row['pct']:5.1f}%  "
            f"{row['used_mb']:5.0f}/{row['total_mb']}MB  {row['temp_c']}°C  "
            f"{row['models']}"
        )

    print(f"\nStats: {stats()}")
