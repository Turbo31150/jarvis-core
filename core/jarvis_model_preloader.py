#!/usr/bin/env python3
"""
jarvis_model_preloader — Predictive model preloading based on usage patterns
Warms up models before they're needed, reduces cold-start latency
"""

import asyncio
import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.model_preloader")

USAGE_FILE = Path("/home/turbo/IA/Core/jarvis/data/model_usage.json")
REDIS_KEY = "jarvis:preloader"

BACKENDS = {
    "M1": "http://127.0.0.1:1234",
    "M2": "http://192.168.1.26:1234",
}

# Time-of-day buckets (hour -> expected models)
SCHEDULE: dict[int, list[str]] = {
    # Morning: trading + analysis
    7: ["qwen/qwen3.5-9b", "qwen/qwen3.5-35b-a3b"],
    8: ["qwen/qwen3.5-9b", "qwen/qwen3.5-35b-a3b"],
    9: ["qwen/qwen3.5-9b"],
    # Afternoon: dev work
    14: ["qwen/qwen3.5-9b", "qwen3-14b"],
    15: ["qwen/qwen3.5-9b"],
    16: ["qwen/qwen3.5-9b", "deepseek/deepseek-r1-0528-qwen3-8b"],
    # Evening: heavy analysis
    20: ["qwen/qwen3.5-35b-a3b", "deepseek/deepseek-r1-0528-qwen3-8b"],
    21: ["qwen/qwen3.5-35b-a3b"],
}


@dataclass
class ModelUsageStats:
    model_id: str
    backend: str
    request_count: int = 0
    last_used: float = 0.0
    hourly_counts: dict[int, int] = field(default_factory=lambda: defaultdict(int))
    avg_load_time_s: float = 0.0
    is_loaded: bool = False

    def predict_next_hour(self, hour: int) -> float:
        """Predict usage probability for given hour (0.0-1.0)."""
        total = sum(self.hourly_counts.values())
        if total == 0:
            return 0.0
        hour_count = self.hourly_counts.get(hour, 0)
        return hour_count / total

    def to_dict(self) -> dict:
        return {
            "model_id": self.model_id,
            "backend": self.backend,
            "request_count": self.request_count,
            "last_used": self.last_used,
            "hourly_counts": dict(self.hourly_counts),
            "avg_load_time_s": self.avg_load_time_s,
        }


class ModelPreloader:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._stats: dict[str, ModelUsageStats] = {}
        self._load()

    def _load(self):
        if USAGE_FILE.exists():
            try:
                data = json.loads(USAGE_FILE.read_text())
                for key, d in data.items():
                    s = ModelUsageStats(
                        model_id=d["model_id"],
                        backend=d["backend"],
                        request_count=d["request_count"],
                        last_used=d["last_used"],
                        avg_load_time_s=d.get("avg_load_time_s", 0),
                    )
                    s.hourly_counts = defaultdict(
                        int, {int(h): c for h, c in d.get("hourly_counts", {}).items()}
                    )
                    self._stats[key] = s
            except Exception as e:
                log.warning(f"Load usage stats error: {e}")

    def _save(self):
        USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        USAGE_FILE.write_text(
            json.dumps({k: v.to_dict() for k, v in self._stats.items()}, indent=2)
        )

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def record_usage(self, model_id: str, backend: str):
        key = f"{backend}:{model_id}"
        if key not in self._stats:
            self._stats[key] = ModelUsageStats(model_id=model_id, backend=backend)
        s = self._stats[key]
        s.request_count += 1
        s.last_used = time.time()
        hour = int(time.strftime("%H"))
        s.hourly_counts[hour] += 1
        self._save()

    async def get_loaded_models(self, backend: str) -> set[str]:
        url = BACKENDS.get(backend)
        if not url:
            return set()
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5)
            ) as sess:
                async with sess.get(f"{url}/v1/models") as r:
                    if r.status != 200:
                        return set()
                    data = await r.json()
                    return {m["id"] for m in data.get("data", [])}
        except Exception:
            return set()

    async def warm_up(self, model_id: str, backend: str) -> dict:
        """Send a minimal request to warm up the model."""
        url = BACKENDS.get(backend)
        if not url:
            return {"ok": False, "error": "unknown backend"}

        t0 = time.time()
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60)
            ) as sess:
                async with sess.post(
                    f"{url}/v1/chat/completions",
                    json={
                        "model": model_id,
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 1,
                        "temperature": 0,
                    },
                ) as r:
                    ok = r.status == 200
                    elapsed = time.time() - t0

            key = f"{backend}:{model_id}"
            if key in self._stats:
                s = self._stats[key]
                alpha = 0.3
                s.avg_load_time_s = alpha * elapsed + (1 - alpha) * s.avg_load_time_s

            log.info(f"Warmed up {model_id} on {backend}: {elapsed:.2f}s")
            return {
                "ok": ok,
                "backend": backend,
                "model": model_id,
                "load_time_s": round(elapsed, 2),
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def predict_needed(self, look_ahead_hours: int = 1) -> list[tuple[str, str]]:
        """Predict which models will be needed in the next N hours."""
        now_hour = int(time.strftime("%H"))
        needed = set()

        # Schedule-based
        for h in range(now_hour + 1, now_hour + look_ahead_hours + 1):
            h_mod = h % 24
            for model in SCHEDULE.get(h_mod, []):
                needed.add(("M1", model))

        # Usage pattern-based
        for key, stats in self._stats.items():
            for h in range(now_hour + 1, now_hour + look_ahead_hours + 1):
                prob = stats.predict_next_hour(h % 24)
                if prob > 0.3:  # 30% probability threshold
                    needed.add((stats.backend, stats.model_id))

        return list(needed)

    async def preload_predicted(self) -> list[dict]:
        """Preload models predicted to be needed soon."""
        needed = self.predict_needed()
        results = []

        for backend, model_id in needed:
            loaded = await self.get_loaded_models(backend)
            if model_id not in loaded:
                log.info(f"Preloading predicted model: {model_id} on {backend}")
                result = await self.warm_up(model_id, backend)
                results.append(result)

        return results

    async def run_loop(self, interval_s: int = 1800):
        """Background loop: check and preload every interval."""
        await self.connect_redis()
        log.info("Model preloader started")
        while True:
            try:
                results = await self.preload_predicted()
                if results:
                    log.info(f"Preloaded {len(results)} models")
                    if self.redis:
                        await self.redis.set(
                            REDIS_KEY,
                            json.dumps(
                                {
                                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                    "preloaded": results,
                                }
                            ),
                            ex=3600,
                        )
            except Exception as e:
                log.error(f"Preloader error: {e}")
            await asyncio.sleep(interval_s)


# ── CLI ───────────────────────────────────────────────────────────────────────


async def main():
    import sys

    loader = ModelPreloader()
    await loader.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "predict"

    if cmd == "predict":
        needed = loader.predict_needed(look_ahead_hours=2)
        print("Predicted models needed in next 2h:")
        for backend, model in needed:
            print(f"  [{backend}] {model}")

    elif cmd == "loaded":
        backend = sys.argv[2] if len(sys.argv) > 2 else "M1"
        models = await loader.get_loaded_models(backend)
        print(f"Loaded on {backend}: {len(models)} models")
        for m in sorted(models):
            print(f"  • {m}")

    elif cmd == "warmup" and len(sys.argv) > 3:
        result = await loader.warm_up(sys.argv[3], sys.argv[2])
        print(json.dumps(result, indent=2))

    elif cmd == "preload":
        results = await loader.preload_predicted()
        if results:
            for r in results:
                print(
                    f"  [{r['backend']}] {r.get('model')}: {'✅' if r['ok'] else '❌'} {r.get('load_time_s', '')}s"
                )
        else:
            print("All predicted models already loaded")

    elif cmd == "stats":
        print(f"{'Model':<40} {'Backend':<6} {'Requests':>9} {'Avg Load':>9}")
        print("-" * 68)
        for s in sorted(loader._stats.values(), key=lambda x: -x.request_count)[:20]:
            print(
                f"{s.model_id:<40} {s.backend:<6} {s.request_count:>9} "
                f"{s.avg_load_time_s:>8.2f}s"
            )

    elif cmd == "daemon":
        logging.basicConfig(level=logging.INFO)
        await loader.run_loop()


if __name__ == "__main__":
    asyncio.run(main())
