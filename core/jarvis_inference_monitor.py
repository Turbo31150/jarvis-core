#!/usr/bin/env python3
"""
jarvis_inference_monitor — Real-time LLM inference monitoring and alerting
Tracks active requests, detects stalls, measures throughput, fires alerts
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from statistics import mean

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.inference_monitor")

REDIS_PREFIX = "jarvis:infer_mon:"
STALL_THRESHOLD_S = 30.0  # alert if request takes > 30s with no tokens
SLOW_THRESHOLD_S = 10.0  # warn if TTFT > 10s
MIN_TOK_PER_S = 5.0  # alert if throughput drops below 5 tok/s


@dataclass
class InferenceRequest:
    request_id: str
    model: str
    backend: str
    started_at: float
    first_token_at: float = 0.0
    last_token_at: float = 0.0
    tokens_out: int = 0
    status: str = "active"  # active | completed | stalled | error
    error: str = ""

    @property
    def ttft_ms(self) -> float:
        if not self.first_token_at:
            return (time.time() - self.started_at) * 1000
        return (self.first_token_at - self.started_at) * 1000

    @property
    def elapsed_s(self) -> float:
        return time.time() - self.started_at

    @property
    def tok_per_s(self) -> float:
        if not self.first_token_at or not self.tokens_out:
            return 0.0
        elapsed = (self.last_token_at or time.time()) - self.first_token_at
        return self.tokens_out / max(elapsed, 0.001)

    @property
    def is_stalled(self) -> bool:
        if self.status != "active":
            return False
        since_last = time.time() - max(
            self.last_token_at or self.started_at, self.started_at
        )
        return since_last > STALL_THRESHOLD_S

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "model": self.model,
            "backend": self.backend,
            "status": self.status,
            "elapsed_s": round(self.elapsed_s, 1),
            "ttft_ms": round(self.ttft_ms, 1),
            "tokens_out": self.tokens_out,
            "tok_per_s": round(self.tok_per_s, 1),
            "stalled": self.is_stalled,
            "error": self.error,
        }


class InferenceMonitor:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._active: dict[str, InferenceRequest] = {}
        self._completed: list[InferenceRequest] = []
        self._alert_callbacks: list = []
        self._monitor_task: asyncio.Task | None = None

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def on_alert(self, callback):
        self._alert_callbacks.append(callback)

    async def _fire_alert(
        self, level: str, message: str, req: InferenceRequest | None = None
    ):
        log.warning(f"[{level}] {message}")
        if self.redis:
            await self.redis.lpush(
                f"{REDIS_PREFIX}alerts",
                json.dumps(
                    {
                        "level": level,
                        "message": message,
                        "request": req.to_dict() if req else None,
                        "ts": time.time(),
                    }
                ),
            )
            await self.redis.ltrim(f"{REDIS_PREFIX}alerts", 0, 499)
            await self.redis.publish(
                "jarvis:events",
                json.dumps(
                    {"event": "inference_alert", "level": level, "message": message}
                ),
            )
        for cb in self._alert_callbacks:
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(level, message, req)
                else:
                    cb(level, message, req)
            except Exception as e:
                log.error(f"Alert callback error: {e}")

    def start_request(
        self, request_id: str, model: str, backend: str = "M1"
    ) -> InferenceRequest:
        req = InferenceRequest(
            request_id=request_id,
            model=model,
            backend=backend,
            started_at=time.time(),
        )
        self._active[request_id] = req
        return req

    def record_token(self, request_id: str):
        req = self._active.get(request_id)
        if not req:
            return
        now = time.time()
        if not req.first_token_at:
            req.first_token_at = now
        req.last_token_at = now
        req.tokens_out += 1

    async def end_request(
        self,
        request_id: str,
        status: str = "completed",
        error: str = "",
    ) -> InferenceRequest | None:
        req = self._active.pop(request_id, None)
        if not req:
            return None
        req.status = status
        req.error = error

        self._completed.append(req)
        if len(self._completed) > 5000:
            self._completed = self._completed[-5000:]

        # Check for slow TTFT
        if req.ttft_ms > SLOW_THRESHOLD_S * 1000:
            await self._fire_alert(
                "WARN", f"Slow TTFT {req.ttft_ms:.0f}ms for {req.model}", req
            )

        # Check for low throughput
        if req.tok_per_s > 0 and req.tok_per_s < MIN_TOK_PER_S:
            await self._fire_alert(
                "WARN", f"Low throughput {req.tok_per_s:.1f} tok/s for {req.model}", req
            )

        if self.redis:
            await self.redis.hincrby(f"{REDIS_PREFIX}model:{req.model}", "requests", 1)
            await self.redis.hincrbyfloat(
                f"{REDIS_PREFIX}model:{req.model}", "total_tokens", req.tokens_out
            )

        return req

    async def start_stall_monitor(self, interval_s: float = 5.0):
        async def loop():
            while True:
                await asyncio.sleep(interval_s)
                for req in list(self._active.values()):
                    if req.is_stalled:
                        await self._fire_alert(
                            "ERROR",
                            f"Request {req.request_id} STALLED for {req.elapsed_s:.0f}s "
                            f"(model={req.model})",
                            req,
                        )
                        req.status = "stalled"

                # Sync stats to Redis
                if self.redis and self._active:
                    await self.redis.set(
                        f"{REDIS_PREFIX}active",
                        json.dumps([r.to_dict() for r in self._active.values()]),
                        ex=60,
                    )

        self._monitor_task = asyncio.create_task(loop())

    def snapshot(self) -> dict:
        active = list(self._active.values())
        recent = self._completed[-100:] if self._completed else []
        ok = [r for r in recent if r.status == "completed"]
        return {
            "active_requests": len(active),
            "active": [r.to_dict() for r in active],
            "completed_last_100": len(recent),
            "avg_ttft_ms": round(mean(r.ttft_ms for r in ok), 1) if ok else 0,
            "avg_tok_per_s": round(mean(r.tok_per_s for r in ok if r.tok_per_s > 0), 1)
            if ok
            else 0,
            "stalled": sum(1 for r in active if r.is_stalled),
            "errors": sum(1 for r in recent if r.status == "error"),
        }

    async def probe_backends(self) -> dict[str, dict]:
        """Quick health probe on inference backends."""
        backends = {
            "M1": "http://127.0.0.1:1234",
            "M2": "http://192.168.1.26:1234",
        }
        results = {}
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=3)
        ) as sess:
            for name, url in backends.items():
                t0 = time.time()
                try:
                    async with sess.get(f"{url}/v1/models") as r:
                        latency_ms = (time.time() - t0) * 1000
                        up = r.status == 200
                        data = await r.json() if up else {}
                        results[name] = {
                            "up": up,
                            "latency_ms": round(latency_ms, 1),
                            "models": len(data.get("data", [])),
                        }
                except Exception as e:
                    results[name] = {"up": False, "error": str(e)[:50]}
        return results


async def main():
    import sys
    import uuid

    mon = InferenceMonitor()
    await mon.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        import random

        print("Simulating 5 inference requests...")
        for i in range(5):
            rid = str(uuid.uuid4())[:8]
            req = mon.start_request(rid, "qwen3.5-9b", "M1")
            await asyncio.sleep(random.uniform(0.05, 0.3))
            req.first_token_at = time.time()
            for _ in range(random.randint(20, 100)):
                mon.record_token(rid)
                await asyncio.sleep(0.01)
            result = await mon.end_request(rid)
            print(
                f"  [{rid}] {result.tokens_out}t {result.ttft_ms:.0f}ms TTFT {result.tok_per_s:.1f}tok/s"
            )

        snap = mon.snapshot()
        print(
            f"\nAvg TTFT: {snap['avg_ttft_ms']}ms | Avg throughput: {snap['avg_tok_per_s']} tok/s"
        )

    elif cmd == "probe":
        results = await mon.probe_backends()
        for name, info in results.items():
            status = "✅" if info.get("up") else "❌"
            print(f"  {status} {name}: {info}")

    elif cmd == "snapshot":
        print(json.dumps(mon.snapshot(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
