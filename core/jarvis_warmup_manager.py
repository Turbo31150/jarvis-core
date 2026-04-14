#!/usr/bin/env python3
"""
jarvis_warmup_manager — Model and service warmup orchestration
Pre-loads models, pre-warms connections, runs smoke tests before traffic
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.warmup_manager")

REDIS_PREFIX = "jarvis:warmup:"


class WarmupStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    READY = "ready"
    FAILED = "failed"
    SKIPPED = "skipped"


class WarmupKind(str, Enum):
    LLM_MODEL = "llm_model"  # ping /v1/models + inference smoke test
    EMBEDDING = "embedding"  # ping embedding endpoint
    REDIS = "redis"  # ping Redis
    HTTP_SERVICE = "http_service"  # GET healthcheck URL
    CUSTOM = "custom"  # user-provided coroutine


@dataclass
class WarmupTarget:
    name: str
    kind: WarmupKind
    endpoint_url: str = ""
    model: str = ""
    healthcheck_path: str = "/health"
    smoke_prompt: str = "Hello"
    timeout_s: float = 30.0
    retries: int = 2
    retry_delay_s: float = 3.0
    priority: int = 0  # higher = warmed up first
    depends_on: list[str] = field(default_factory=list)
    custom_fn: Callable | None = field(default=None, repr=False)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind.value,
            "endpoint_url": self.endpoint_url,
            "model": self.model,
            "priority": self.priority,
            "depends_on": self.depends_on,
        }


@dataclass
class WarmupResult:
    name: str
    status: WarmupStatus
    latency_ms: float
    attempts: int
    error: str = ""
    ts: float = field(default_factory=time.time)

    @property
    def ready(self) -> bool:
        return self.status == WarmupStatus.READY

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status.value,
            "latency_ms": round(self.latency_ms, 1),
            "attempts": self.attempts,
            "error": self.error,
            "ts": self.ts,
        }


async def _warmup_llm(target: WarmupTarget) -> WarmupResult:
    t0 = time.time()
    attempts = 0
    last_error = ""

    for attempt in range(1, target.retries + 2):
        attempts = attempt
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=target.timeout_s)
            ) as sess:
                # Step 1: list models
                async with sess.get(f"{target.endpoint_url}/v1/models") as r:
                    if r.status != 200:
                        raise RuntimeError(f"Models endpoint HTTP {r.status}")
                    data = await r.json(content_type=None)
                    model_ids = [m.get("id", "") for m in data.get("data", [])]
                    if target.model and target.model not in model_ids:
                        raise RuntimeError(
                            f"Model '{target.model}' not found in {model_ids[:5]}"
                        )

                # Step 2: smoke inference
                payload = {
                    "model": target.model or (model_ids[0] if model_ids else "default"),
                    "messages": [{"role": "user", "content": target.smoke_prompt}],
                    "max_tokens": 8,
                    "temperature": 0.0,
                }
                async with sess.post(
                    f"{target.endpoint_url}/v1/chat/completions", json=payload
                ) as r2:
                    if r2.status != 200:
                        text = await r2.text()
                        raise RuntimeError(f"Inference HTTP {r2.status}: {text[:80]}")

            return WarmupResult(
                name=target.name,
                status=WarmupStatus.READY,
                latency_ms=(time.time() - t0) * 1000,
                attempts=attempts,
            )
        except Exception as e:
            last_error = str(e)[:200]
            if attempt <= target.retries:
                await asyncio.sleep(target.retry_delay_s)

    return WarmupResult(
        name=target.name,
        status=WarmupStatus.FAILED,
        latency_ms=(time.time() - t0) * 1000,
        attempts=attempts,
        error=last_error,
    )


async def _warmup_http(target: WarmupTarget) -> WarmupResult:
    t0 = time.time()
    url = f"{target.endpoint_url}{target.healthcheck_path}"
    for attempt in range(1, target.retries + 2):
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=target.timeout_s)
            ) as sess:
                async with sess.get(url) as r:
                    if r.status < 400:
                        return WarmupResult(
                            name=target.name,
                            status=WarmupStatus.READY,
                            latency_ms=(time.time() - t0) * 1000,
                            attempts=attempt,
                        )
                    raise RuntimeError(f"HTTP {r.status}")
        except Exception as e:
            if attempt <= target.retries:
                await asyncio.sleep(target.retry_delay_s)
            last_error = str(e)[:200]

    return WarmupResult(
        name=target.name,
        status=WarmupStatus.FAILED,
        latency_ms=(time.time() - t0) * 1000,
        attempts=target.retries + 1,
        error=last_error,
    )


async def _warmup_redis(target: WarmupTarget) -> WarmupResult:
    t0 = time.time()
    try:
        r = aioredis.Redis.from_url(target.endpoint_url or "redis://localhost:6379")
        await asyncio.wait_for(r.ping(), timeout=target.timeout_s)
        await r.aclose()
        return WarmupResult(
            name=target.name,
            status=WarmupStatus.READY,
            latency_ms=(time.time() - t0) * 1000,
            attempts=1,
        )
    except Exception as e:
        return WarmupResult(
            name=target.name,
            status=WarmupStatus.FAILED,
            latency_ms=(time.time() - t0) * 1000,
            attempts=1,
            error=str(e)[:200],
        )


async def _run_target(target: WarmupTarget) -> WarmupResult:
    if target.kind == WarmupKind.LLM_MODEL:
        return await _warmup_llm(target)
    elif target.kind in (WarmupKind.HTTP_SERVICE, WarmupKind.EMBEDDING):
        return await _warmup_http(target)
    elif target.kind == WarmupKind.REDIS:
        return await _warmup_redis(target)
    elif target.kind == WarmupKind.CUSTOM and target.custom_fn:
        t0 = time.time()
        try:
            await target.custom_fn(target)
            return WarmupResult(
                name=target.name,
                status=WarmupStatus.READY,
                latency_ms=(time.time() - t0) * 1000,
                attempts=1,
            )
        except Exception as e:
            return WarmupResult(
                name=target.name,
                status=WarmupStatus.FAILED,
                latency_ms=(time.time() - t0) * 1000,
                attempts=1,
                error=str(e)[:200],
            )
    return WarmupResult(
        name=target.name,
        status=WarmupStatus.SKIPPED,
        latency_ms=0.0,
        attempts=0,
        error="No handler",
    )


class WarmupManager:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._targets: dict[str, WarmupTarget] = {}
        self._results: dict[str, WarmupResult] = {}
        self._ready_event = asyncio.Event()
        self._stats: dict[str, int] = {
            "warmups_run": 0,
            "ready": 0,
            "failed": 0,
            "skipped": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def add(self, target: WarmupTarget):
        self._targets[target.name] = target

    def is_ready(self, name: str) -> bool:
        result = self._results.get(name)
        return result is not None and result.ready

    def all_ready(self) -> bool:
        return all(
            self._results.get(
                t.name, WarmupResult("", WarmupStatus.PENDING, 0, 0)
            ).ready
            for t in self._targets.values()
        )

    async def run_all(self, concurrency: int = 4) -> dict[str, WarmupResult]:
        """Warm up all targets respecting dependency order."""
        # Topological sort by dependencies
        completed: set[str] = set()
        remaining = list(self._targets.values())
        sem = asyncio.Semaphore(concurrency)

        async def run_one(t: WarmupTarget):
            # Wait for deps
            for dep in t.depends_on:
                while dep not in completed:
                    await asyncio.sleep(0.1)
            async with sem:
                log.info(f"Warming up: {t.name} ({t.kind.value})")
                result = await _run_target(t)
                self._results[t.name] = result
                self._stats["warmups_run"] += 1
                if result.ready:
                    self._stats["ready"] += 1
                    completed.add(t.name)
                    log.info(f"  ✓ {t.name} ready ({result.latency_ms:.0f}ms)")
                elif result.status == WarmupStatus.SKIPPED:
                    self._stats["skipped"] += 1
                    completed.add(t.name)
                else:
                    self._stats["failed"] += 1
                    completed.add(t.name)  # unblock dependents even on failure
                    log.warning(f"  ✗ {t.name} FAILED: {result.error}")

                if self.redis:
                    asyncio.create_task(
                        self.redis.setex(
                            f"{REDIS_PREFIX}{t.name}",
                            300,
                            json.dumps(result.to_dict()),
                        )
                    )

        # Sort by priority descending, then run
        sorted_targets = sorted(remaining, key=lambda t: -t.priority)
        await asyncio.gather(*[run_one(t) for t in sorted_targets])

        if self.all_ready():
            self._ready_event.set()

        return self._results

    async def wait_ready(self, timeout_s: float = 120.0):
        await asyncio.wait_for(self._ready_event.wait(), timeout=timeout_s)

    def summary(self) -> list[dict]:
        return [r.to_dict() for r in sorted(self._results.values(), key=lambda r: r.ts)]

    def stats(self) -> dict:
        return {
            **self._stats,
            "targets": len(self._targets),
            "all_ready": self.all_ready(),
        }


def build_jarvis_warmup_manager() -> WarmupManager:
    wm = WarmupManager()

    wm.add(
        WarmupTarget(
            name="redis",
            kind=WarmupKind.REDIS,
            endpoint_url="redis://localhost:6379",
            priority=10,
        )
    )
    wm.add(
        WarmupTarget(
            name="m1-qwen9b",
            kind=WarmupKind.LLM_MODEL,
            endpoint_url="http://192.168.1.85:1234",
            model="qwen3.5-9b",
            smoke_prompt="Hi",
            priority=8,
            depends_on=["redis"],
        )
    )
    wm.add(
        WarmupTarget(
            name="m2-deepseek",
            kind=WarmupKind.LLM_MODEL,
            endpoint_url="http://192.168.1.26:1234",
            model="deepseek-r1-0528",
            smoke_prompt="Hi",
            priority=7,
            depends_on=["redis"],
        )
    )
    wm.add(
        WarmupTarget(
            name="ol1-gemma",
            kind=WarmupKind.LLM_MODEL,
            endpoint_url="http://127.0.0.1:11434",
            model="gemma3:4b",
            smoke_prompt="Hi",
            priority=6,
        )
    )
    return wm


async def main():
    import sys

    wm = build_jarvis_warmup_manager()
    await wm.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"

    if cmd == "run":
        print("Running warmup sequence...")
        t0 = time.time()
        results = await wm.run_all(concurrency=3)
        elapsed = time.time() - t0

        print(f"\nWarmup complete in {elapsed:.1f}s:")
        for r in wm.summary():
            icon = (
                "✅"
                if r["status"] == "ready"
                else ("⏭️" if r["status"] == "skipped" else "❌")
            )
            print(
                f"  {icon} {r['name']:<20} {r['status']:<12} "
                f"{r['latency_ms']:.0f}ms attempts={r['attempts']}"
            )
            if r["error"]:
                print(f"       {r['error'][:80]}")

        print(f"\nAll ready: {wm.all_ready()}")
        print(f"Stats: {json.dumps(wm.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(wm.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
