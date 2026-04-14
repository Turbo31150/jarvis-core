#!/usr/bin/env python3
"""
jarvis_kv_cache_manager — KV-cache allocation tracker across GPUs
Monitors llama.cpp KV cache usage, predicts eviction, triggers prefill
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.kv_cache")

BACKENDS = {
    "M1": "http://127.0.0.1:1234",
    "M2": "http://192.168.1.26:1234",
}

REDIS_KEY = "jarvis:kv_cache"
# llama.cpp /health and /metrics endpoints
LLAMA_METRICS_PORT = 8080  # ik_llama.cpp default


@dataclass
class KVSlot:
    slot_id: int
    state: str  # idle / active / evicted
    tokens_used: int = 0
    last_used: float = 0.0


@dataclass
class CacheStats:
    backend: str
    n_ctx: int = 0
    n_ctx_used: int = 0
    cache_pct: float = 0.0
    slots: list[KVSlot] = field(default_factory=list)
    ts: float = field(default_factory=time.time)

    @property
    def pressure(self) -> str:
        if self.cache_pct >= 90:
            return "CRITICAL"
        if self.cache_pct >= 75:
            return "HIGH"
        if self.cache_pct >= 50:
            return "MEDIUM"
        return "LOW"


class KVCacheManager:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self.stats: dict[str, CacheStats] = {}

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    async def fetch_metrics(self, name: str, base_url: str) -> CacheStats:
        """Fetch /metrics from llama.cpp server (Prometheus format)."""
        stats = CacheStats(backend=name)
        metrics_url = f"{base_url.rstrip('/')}/metrics"

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5)
            ) as sess:
                async with sess.get(metrics_url) as r:
                    if r.status != 200:
                        return stats
                    text = await r.text()

            for line in text.splitlines():
                if line.startswith("#") or not line.strip():
                    continue
                if "llama_kv_cache_tokens_count" in line and "{" not in line:
                    try:
                        stats.n_ctx_used = int(float(line.split()[-1]))
                    except ValueError:
                        pass
                elif "llama_kv_cache_max_tokens_count" in line and "{" not in line:
                    try:
                        stats.n_ctx = int(float(line.split()[-1]))
                    except ValueError:
                        pass

            if stats.n_ctx > 0:
                stats.cache_pct = round(stats.n_ctx_used / stats.n_ctx * 100, 1)

        except Exception as e:
            log.debug(f"Metrics fetch {name}: {e}")

        return stats

    async def fetch_slots(self, name: str, base_url: str) -> list[KVSlot]:
        """Fetch /slots from llama.cpp server."""
        slots = []
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5)
            ) as sess:
                async with sess.get(f"{base_url}/slots") as r:
                    if r.status != 200:
                        return slots
                    data = await r.json()

            for s in data:
                slots.append(
                    KVSlot(
                        slot_id=s.get("id", 0),
                        state="active" if s.get("is_processing") else "idle",
                        tokens_used=s.get("n_past", 0),
                        last_used=time.time(),
                    )
                )
        except Exception:
            pass
        return slots

    async def collect_all(self) -> dict[str, CacheStats]:
        tasks = [self.fetch_metrics(n, url) for n, url in BACKENDS.items()]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for i, name in enumerate(BACKENDS):
            if not isinstance(results[i], Exception):
                self.stats[name] = results[i]
        return self.stats

    def eviction_risk(self, stats: CacheStats) -> dict:
        """Estimate eviction risk and recommend action."""
        risk = {
            "backend": stats.backend,
            "cache_pct": stats.cache_pct,
            "pressure": stats.pressure,
            "action": None,
        }
        if stats.pressure == "CRITICAL":
            risk["action"] = "FLUSH_IDLE_SLOTS"
        elif stats.pressure == "HIGH":
            risk["action"] = "REDUCE_CONTEXT_LENGTH"
        elif stats.pressure == "MEDIUM":
            risk["action"] = "MONITOR"
        return risk

    async def report(self) -> dict:
        await self.collect_all()
        result = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "backends": {},
        }
        for name, stats in self.stats.items():
            result["backends"][name] = {
                "n_ctx": stats.n_ctx,
                "n_ctx_used": stats.n_ctx_used,
                "cache_pct": stats.cache_pct,
                "pressure": stats.pressure,
                "risk": self.eviction_risk(stats),
            }

        if self.redis:
            await self.redis.set(REDIS_KEY, json.dumps(result), ex=30)

        return result

    async def flush_idle_slots(self, backend_url: str) -> bool:
        """POST /slots/{id}/action to flush idle slots."""
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            ) as sess:
                # Get slots first
                async with sess.get(f"{backend_url}/slots") as r:
                    if r.status != 200:
                        return False
                    slots = await r.json()

                flushed = 0
                for s in slots:
                    if not s.get("is_processing", True):
                        slot_id = s.get("id", 0)
                        async with sess.post(
                            f"{backend_url}/slots/{slot_id}",
                            json={"action": "erase"},
                        ) as resp:
                            if resp.status == 200:
                                flushed += 1

                log.info(f"Flushed {flushed} idle slots on {backend_url}")
                return flushed > 0
        except Exception as e:
            log.error(f"Flush failed: {e}")
            return False


# ── CLI ───────────────────────────────────────────────────────────────────────


async def main():
    import sys

    mgr = KVCacheManager()
    await mgr.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"

    if cmd == "report":
        r = await mgr.report()
        print(json.dumps(r, indent=2))

    elif cmd == "flush":
        backend = sys.argv[2] if len(sys.argv) > 2 else "M1"
        url = BACKENDS.get(backend, BACKENDS["M1"])
        ok = await mgr.flush_idle_slots(url)
        print(f"Flush {backend}: {'OK' if ok else 'FAILED'}")

    elif cmd == "watch":
        while True:
            r = await mgr.report()
            print(f"\r[{r['ts']}] ", end="")
            for name, info in r["backends"].items():
                print(
                    f"{name}: {info['cache_pct']}% [{info['pressure']}]  ",
                    end="",
                )
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
