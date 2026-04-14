#!/usr/bin/env python3
"""
jarvis_lifecycle — Model lifecycle manager: load, warm-up, idle detection, unload
Tracks model state transitions, enforces VRAM budget, auto-unloads idle models
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from enum import Enum

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.model_lifecycle")

REDIS_PREFIX = "jarvis:lifecycle:"
LM_URL = "http://127.0.0.1:1234"
IDLE_TIMEOUT_S = 600  # unload after 10min idle
MAX_LOADED_MODELS = 3  # max concurrent models in VRAM


class ModelState(str, Enum):
    UNLOADED = "unloaded"
    LOADING = "loading"
    WARMING = "warming"
    READY = "ready"
    BUSY = "busy"
    IDLE = "idle"
    UNLOADING = "unloading"
    ERROR = "error"


@dataclass
class ModelEntry:
    model_id: str
    backend: str  # M1 | M2
    state: ModelState = ModelState.UNLOADED
    loaded_at: float = 0.0
    last_used_at: float = 0.0
    request_count: int = 0
    error_count: int = 0
    vram_mb: int = 0
    warmup_done: bool = False
    priority: int = 5  # 1=high (keep loaded), 10=low (evict first)

    @property
    def idle_s(self) -> float:
        if not self.last_used_at:
            return 0.0
        return time.time() - self.last_used_at

    @property
    def is_idle(self) -> bool:
        return self.state == ModelState.READY and self.idle_s > IDLE_TIMEOUT_S

    def to_dict(self) -> dict:
        return {
            "model_id": self.model_id,
            "backend": self.backend,
            "state": self.state.value,
            "loaded_at": self.loaded_at,
            "last_used_at": self.last_used_at,
            "request_count": self.request_count,
            "error_count": self.error_count,
            "vram_mb": self.vram_mb,
            "warmup_done": self.warmup_done,
            "priority": self.priority,
            "idle_s": round(self.idle_s, 1),
        }


class ModelLifecycle:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._models: dict[str, ModelEntry] = {}
        self._lifecycle_task: asyncio.Task | None = None
        self._callbacks: dict[str, list] = {}  # state → [callback]

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def on_state_change(self, state: ModelState, callback):
        self._callbacks.setdefault(state.value, []).append(callback)

    async def _transition(self, entry: ModelEntry, new_state: ModelState):
        old = entry.state
        entry.state = new_state
        log.info(f"[{entry.model_id}] {old.value} → {new_state.value}")

        await self._sync(entry)

        for cb in self._callbacks.get(new_state.value, []):
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(entry)
                else:
                    cb(entry)
            except Exception as e:
                log.error(f"State callback error: {e}")

        if self.redis:
            await self.redis.publish(
                "jarvis:events",
                json.dumps(
                    {
                        "event": "model_state_change",
                        "model": entry.model_id,
                        "from": old.value,
                        "to": new_state.value,
                        "ts": time.time(),
                    }
                ),
            )

    async def _sync(self, entry: ModelEntry):
        if self.redis:
            await self.redis.hset(
                f"{REDIS_PREFIX}models",
                entry.model_id,
                json.dumps(entry.to_dict()),
            )

    async def ensure_loaded(self, model_id: str, backend: str = "M1") -> ModelEntry:
        """Ensure model is ready; load if needed."""
        entry = self._models.get(model_id)

        if entry and entry.state == ModelState.READY:
            entry.last_used_at = time.time()
            await self._sync(entry)
            return entry

        if entry and entry.state == ModelState.LOADING:
            # Wait for loading to complete
            for _ in range(60):
                await asyncio.sleep(1.0)
                if self._models[model_id].state == ModelState.READY:
                    return self._models[model_id]
            raise TimeoutError(f"Model {model_id} loading timeout")

        return await self.load(model_id, backend)

    async def load(
        self, model_id: str, backend: str = "M1", priority: int = 5
    ) -> ModelEntry:
        if model_id in self._models and self._models[model_id].state in (
            ModelState.READY,
            ModelState.BUSY,
            ModelState.WARMING,
        ):
            return self._models[model_id]

        # Evict if at capacity
        await self._maybe_evict(model_id)

        entry = ModelEntry(model_id=model_id, backend=backend, priority=priority)
        self._models[model_id] = entry
        await self._transition(entry, ModelState.LOADING)

        # Trigger model load via LM Studio (load by making a request)
        try:
            backend_url = (
                "http://127.0.0.1:1234"
                if backend == "M1"
                else "http://192.168.1.26:1234"
            )
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=120)
            ) as sess:
                async with sess.post(
                    f"{backend_url}/v1/chat/completions",
                    json={
                        "model": model_id,
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 1,
                    },
                ) as r:
                    if r.status == 200:
                        entry.loaded_at = time.time()
                        entry.last_used_at = time.time()
                        await self._transition(entry, ModelState.WARMING)
                        await self._warmup(entry, backend_url)
                        await self._transition(entry, ModelState.READY)
                    else:
                        raise RuntimeError(f"HTTP {r.status}")
        except Exception as e:
            entry.error_count += 1
            log.error(f"Load failed [{model_id}]: {e}")
            await self._transition(entry, ModelState.ERROR)
            raise

        return entry

    async def _warmup(self, entry: ModelEntry, backend_url: str):
        warmup_prompts = ["What is 2+2?", "Hello"]
        for prompt in warmup_prompts:
            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as sess:
                    async with sess.post(
                        f"{backend_url}/v1/chat/completions",
                        json={
                            "model": entry.model_id,
                            "messages": [{"role": "user", "content": prompt}],
                            "max_tokens": 10,
                        },
                    ) as r:
                        if r.status == 200:
                            entry.request_count += 1
            except Exception:
                pass
        entry.warmup_done = True

    async def mark_busy(self, model_id: str):
        entry = self._models.get(model_id)
        if entry and entry.state == ModelState.READY:
            await self._transition(entry, ModelState.BUSY)

    async def mark_ready(self, model_id: str):
        entry = self._models.get(model_id)
        if entry and entry.state == ModelState.BUSY:
            entry.request_count += 1
            entry.last_used_at = time.time()
            await self._transition(entry, ModelState.READY)

    async def unload(self, model_id: str, reason: str = "manual"):
        entry = self._models.get(model_id)
        if not entry:
            return
        log.info(f"Unloading [{model_id}]: {reason}")
        await self._transition(entry, ModelState.UNLOADING)
        # LM Studio: unloading happens by loading a different model or via API
        # For now just mark as unloaded
        await asyncio.sleep(0.5)
        await self._transition(entry, ModelState.UNLOADED)

    async def _maybe_evict(self, incoming_model: str):
        loaded = [
            e
            for e in self._models.values()
            if e.state in (ModelState.READY, ModelState.IDLE)
            and e.model_id != incoming_model
        ]
        if len(loaded) < MAX_LOADED_MODELS:
            return
        # Evict lowest priority + longest idle
        victim = max(loaded, key=lambda e: (e.priority, e.idle_s))
        log.info(f"Evicting [{victim.model_id}] to make room for [{incoming_model}]")
        await self.unload(victim.model_id, reason="eviction")

    async def start_idle_monitor(self, check_interval_s: float = 60.0):
        async def loop():
            while True:
                await asyncio.sleep(check_interval_s)
                for model_id, entry in list(self._models.items()):
                    if entry.is_idle:
                        log.info(
                            f"Idle timeout: [{model_id}] idle for {entry.idle_s:.0f}s"
                        )
                        await self.unload(model_id, reason="idle_timeout")

        self._lifecycle_task = asyncio.create_task(loop())

    def status(self) -> list[dict]:
        return [
            e.to_dict()
            for e in sorted(
                self._models.values(), key=lambda x: x.loaded_at, reverse=True
            )
        ]


async def main():
    import sys

    lc = ModelLifecycle()
    await lc.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "status":
        models = lc.status()
        if not models:
            print("No models tracked")
        else:
            print(f"{'Model':<40} {'State':<12} {'Reqs':>6} {'Idle':>8}")
            print("-" * 70)
            for m in models:
                print(
                    f"  {m['model_id']:<40} {m['state']:<12} {m['request_count']:>6} {m['idle_s']:>6.0f}s"
                )

    elif cmd == "load" and len(sys.argv) > 2:
        model = sys.argv[2]
        backend = sys.argv[3] if len(sys.argv) > 3 else "M1"
        print(f"Loading {model} on {backend}...")
        entry = await lc.load(model, backend)
        print(f"State: {entry.state.value}")

    elif cmd == "unload" and len(sys.argv) > 2:
        await lc.unload(sys.argv[2], "manual")
        print(f"Unloaded: {sys.argv[2]}")


if __name__ == "__main__":
    asyncio.run(main())
