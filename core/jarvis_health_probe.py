#!/usr/bin/env python3
"""
jarvis_health_probe — Active health probing for cluster services
HTTP/TCP/Redis/LLM probes with configurable thresholds and status history
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

log = logging.getLogger("jarvis.health_probe")

REDIS_PREFIX = "jarvis:probe:"


class ProbeType(str, Enum):
    HTTP = "http"
    TCP = "tcp"
    REDIS = "redis"
    LLM = "llm"
    CUSTOM = "custom"


class ProbeStatus(str, Enum):
    UP = "up"
    DOWN = "down"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


@dataclass
class ProbeConfig:
    name: str
    probe_type: ProbeType
    target: str  # URL or host:port
    interval_s: float = 30.0
    timeout_s: float = 5.0
    healthy_threshold: int = 2  # consecutive successes to mark UP
    unhealthy_threshold: int = 3  # consecutive failures to mark DOWN
    expected_status: int = 200  # for HTTP
    custom_fn: Callable | None = None  # for CUSTOM type


@dataclass
class ProbeResult:
    name: str
    status: ProbeStatus
    latency_ms: float
    ts: float = field(default_factory=time.time)
    error: str = ""
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status.value,
            "latency_ms": round(self.latency_ms, 1),
            "ts": self.ts,
            "error": self.error,
            "detail": self.detail,
        }


@dataclass
class ServiceHealth:
    name: str
    status: ProbeStatus = ProbeStatus.UNKNOWN
    consecutive_ok: int = 0
    consecutive_fail: int = 0
    total_probes: int = 0
    total_failures: int = 0
    last_result: ProbeResult | None = None
    history: list[ProbeResult] = field(default_factory=list)

    @property
    def uptime_pct(self) -> float:
        if self.total_probes == 0:
            return 0.0
        return round((1 - self.total_failures / self.total_probes) * 100, 1)

    @property
    def avg_latency_ms(self) -> float:
        if not self.history:
            return 0.0
        return round(sum(r.latency_ms for r in self.history) / len(self.history), 1)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status.value,
            "uptime_pct": self.uptime_pct,
            "avg_latency_ms": self.avg_latency_ms,
            "consecutive_ok": self.consecutive_ok,
            "consecutive_fail": self.consecutive_fail,
            "total_probes": self.total_probes,
            "last_check": self.last_result.ts if self.last_result else 0,
        }


class HealthProbe:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._configs: dict[str, ProbeConfig] = {}
        self._health: dict[str, ServiceHealth] = {}
        self._probe_tasks: dict[str, asyncio.Task] = {}
        self._on_change: list[Callable] = []
        self._running = False

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def register(self, config: ProbeConfig):
        self._configs[config.name] = config
        self._health[config.name] = ServiceHealth(name=config.name)

    def on_status_change(self, callback: Callable):
        self._on_change.append(callback)

    async def _probe_http(self, cfg: ProbeConfig) -> ProbeResult:
        t0 = time.time()
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=cfg.timeout_s)
            ) as sess:
                async with sess.get(cfg.target) as r:
                    latency = (time.time() - t0) * 1000
                    ok = r.status == cfg.expected_status
                    return ProbeResult(
                        name=cfg.name,
                        status=ProbeStatus.UP if ok else ProbeStatus.DEGRADED,
                        latency_ms=latency,
                        detail={"http_status": r.status},
                    )
        except Exception as e:
            return ProbeResult(
                name=cfg.name,
                status=ProbeStatus.DOWN,
                latency_ms=(time.time() - t0) * 1000,
                error=str(e)[:100],
            )

    async def _probe_tcp(self, cfg: ProbeConfig) -> ProbeResult:
        t0 = time.time()
        try:
            host, port_str = cfg.target.rsplit(":", 1)
            port = int(port_str)
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=cfg.timeout_s
            )
            writer.close()
            await writer.wait_closed()
            return ProbeResult(
                name=cfg.name,
                status=ProbeStatus.UP,
                latency_ms=(time.time() - t0) * 1000,
            )
        except Exception as e:
            return ProbeResult(
                name=cfg.name,
                status=ProbeStatus.DOWN,
                latency_ms=(time.time() - t0) * 1000,
                error=str(e)[:100],
            )

    async def _probe_redis(self, cfg: ProbeConfig) -> ProbeResult:
        t0 = time.time()
        try:
            parts = cfg.target.split(":")
            host, port = parts[0], int(parts[1]) if len(parts) > 1 else 6379
            r = aioredis.Redis(host=host, port=port, decode_responses=True)
            await asyncio.wait_for(r.ping(), timeout=cfg.timeout_s)
            await r.aclose()
            return ProbeResult(
                name=cfg.name,
                status=ProbeStatus.UP,
                latency_ms=(time.time() - t0) * 1000,
            )
        except Exception as e:
            return ProbeResult(
                name=cfg.name,
                status=ProbeStatus.DOWN,
                latency_ms=(time.time() - t0) * 1000,
                error=str(e)[:100],
            )

    async def _probe_llm(self, cfg: ProbeConfig) -> ProbeResult:
        t0 = time.time()
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=cfg.timeout_s)
            ) as sess:
                async with sess.get(f"{cfg.target}/v1/models") as r:
                    latency = (time.time() - t0) * 1000
                    if r.status == 200:
                        data = await r.json(content_type=None)
                        model_count = len(data.get("data", []))
                        return ProbeResult(
                            name=cfg.name,
                            status=ProbeStatus.UP,
                            latency_ms=latency,
                            detail={"models": model_count},
                        )
                    return ProbeResult(
                        name=cfg.name,
                        status=ProbeStatus.DEGRADED,
                        latency_ms=latency,
                        detail={"http_status": r.status},
                    )
        except Exception as e:
            return ProbeResult(
                name=cfg.name,
                status=ProbeStatus.DOWN,
                latency_ms=(time.time() - t0) * 1000,
                error=str(e)[:100],
            )

    async def _run_probe(self, cfg: ProbeConfig) -> ProbeResult:
        if cfg.probe_type == ProbeType.HTTP:
            return await self._probe_http(cfg)
        elif cfg.probe_type == ProbeType.TCP:
            return await self._probe_tcp(cfg)
        elif cfg.probe_type == ProbeType.REDIS:
            return await self._probe_redis(cfg)
        elif cfg.probe_type == ProbeType.LLM:
            return await self._probe_llm(cfg)
        elif cfg.probe_type == ProbeType.CUSTOM and cfg.custom_fn:
            t0 = time.time()
            try:
                result = await cfg.custom_fn(cfg)
                return result
            except Exception as e:
                return ProbeResult(
                    name=cfg.name,
                    status=ProbeStatus.DOWN,
                    latency_ms=(time.time() - t0) * 1000,
                    error=str(e)[:100],
                )
        return ProbeResult(name=cfg.name, status=ProbeStatus.UNKNOWN, latency_ms=0)

    def _update_health(
        self, health: ServiceHealth, result: ProbeResult, cfg: ProbeConfig
    ):
        old_status = health.status
        health.total_probes += 1
        health.last_result = result
        health.history.append(result)
        if len(health.history) > 100:
            health.history = health.history[-100:]

        if result.status == ProbeStatus.UP:
            health.consecutive_ok += 1
            health.consecutive_fail = 0
            if health.consecutive_ok >= cfg.healthy_threshold:
                health.status = ProbeStatus.UP
        else:
            health.consecutive_fail += 1
            health.consecutive_ok = 0
            health.total_failures += 1
            if health.consecutive_fail >= cfg.unhealthy_threshold:
                health.status = ProbeStatus.DOWN
            elif health.status == ProbeStatus.UP:
                health.status = ProbeStatus.DEGRADED

        if health.status != old_status:
            log.info(
                f"Health change: {cfg.name} {old_status.value} → {health.status.value}"
            )
            for cb in self._on_change:
                try:
                    cb(cfg.name, old_status, health.status)
                except Exception:
                    pass

    async def _probe_loop(self, name: str):
        cfg = self._configs[name]
        while self._running:
            result = await self._run_probe(cfg)
            health = self._health[name]
            self._update_health(health, result, cfg)

            if self.redis:
                asyncio.create_task(
                    self.redis.setex(
                        f"{REDIS_PREFIX}{name}",
                        int(cfg.interval_s * 3),
                        json.dumps(health.to_dict()),
                    )
                )
            await asyncio.sleep(cfg.interval_s)

    async def probe_once(self, name: str) -> ProbeResult:
        cfg = self._configs.get(name)
        if not cfg:
            return ProbeResult(
                name=name, status=ProbeStatus.UNKNOWN, latency_ms=0, error="not found"
            )
        result = await self._run_probe(cfg)
        self._update_health(self._health[name], result, cfg)
        return result

    async def probe_all_once(self) -> list[ProbeResult]:
        tasks = [self.probe_once(name) for name in self._configs]
        return await asyncio.gather(*tasks)

    async def start(self):
        self._running = True
        for name in self._configs:
            t = asyncio.create_task(self._probe_loop(name))
            self._probe_tasks[name] = t

    async def stop(self):
        self._running = False
        for t in self._probe_tasks.values():
            t.cancel()

    def get_health(self, name: str) -> ServiceHealth | None:
        return self._health.get(name)

    def all_health(self) -> list[dict]:
        return [h.to_dict() for h in self._health.values()]

    def stats(self) -> dict:
        statuses = [h.status for h in self._health.values()]
        return {
            "services": len(self._health),
            "up": sum(1 for s in statuses if s == ProbeStatus.UP),
            "down": sum(1 for s in statuses if s == ProbeStatus.DOWN),
            "degraded": sum(1 for s in statuses if s == ProbeStatus.DEGRADED),
            "unknown": sum(1 for s in statuses if s == ProbeStatus.UNKNOWN),
        }


def build_jarvis_probes() -> HealthProbe:
    probe = HealthProbe()
    probe.register(
        ProbeConfig(
            "m1_lmstudio", ProbeType.LLM, "http://192.168.1.85:1234", interval_s=30
        )
    )
    probe.register(
        ProbeConfig(
            "m2_lmstudio", ProbeType.LLM, "http://192.168.1.26:1234", interval_s=30
        )
    )
    probe.register(
        ProbeConfig(
            "ol1_ollama",
            ProbeType.HTTP,
            "http://127.0.0.1:11434/api/tags",
            interval_s=30,
        )
    )
    probe.register(
        ProbeConfig("redis_local", ProbeType.REDIS, "127.0.0.1:6379", interval_s=15)
    )
    return probe


async def main():
    import sys

    probes = build_jarvis_probes()
    await probes.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Running one-shot probes...")
        results = await probes.probe_all_once()
        for r in results:
            icon = {"up": "✅", "down": "❌", "degraded": "⚠️", "unknown": "❓"}.get(
                r.status.value, "?"
            )
            print(
                f"  {icon} {r.name:<20} {r.latency_ms:>6.0f}ms  {r.error or r.detail}"
            )

        print(f"\nStats: {json.dumps(probes.stats(), indent=2)}")

    elif cmd == "watch":
        probes.on_status_change(
            lambda name, old, new: print(
                f"STATUS CHANGE: {name} {old.value} → {new.value}"
            )
        )
        await probes.start()
        print("Watching... Ctrl+C to stop")
        try:
            await asyncio.sleep(3600)
        except KeyboardInterrupt:
            pass
        await probes.stop()


if __name__ == "__main__":
    asyncio.run(main())
