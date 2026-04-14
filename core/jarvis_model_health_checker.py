#!/usr/bin/env python3
"""
jarvis_model_health_checker — Active health checks for LLM model endpoints
Probes model endpoints with canary requests, tracks health history, fires alerts
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

log = logging.getLogger("jarvis.model_health_checker")

REDIS_PREFIX = "jarvis:modelhealth:"


class ModelHealth(str, Enum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    OFFLINE = "offline"


@dataclass
class HealthCheckConfig:
    interval_s: float = 30.0
    timeout_s: float = 10.0
    canary_prompt: str = "Reply with exactly: OK"
    expected_content: str = "OK"  # substring expected in response
    healthy_threshold: int = 2  # consecutive successes to mark healthy
    unhealthy_threshold: int = 3  # consecutive failures to mark unhealthy
    latency_warn_ms: float = 3000.0
    latency_crit_ms: float = 8000.0


@dataclass
class ModelEndpoint:
    name: str
    url: str
    model: str
    weight: float = 1.0
    tags: list[str] = field(default_factory=list)
    check_config: HealthCheckConfig = field(default_factory=HealthCheckConfig)


@dataclass
class CheckResult:
    endpoint: str
    model: str
    success: bool
    latency_ms: float
    health: ModelHealth
    error: str = ""
    response_snippet: str = ""
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "endpoint": self.endpoint,
            "model": self.model,
            "success": self.success,
            "latency_ms": round(self.latency_ms, 1),
            "health": self.health.value,
            "error": self.error,
            "ts": self.ts,
        }


@dataclass
class EndpointState:
    endpoint: ModelEndpoint
    health: ModelHealth = ModelHealth.UNKNOWN
    consecutive_successes: int = 0
    consecutive_failures: int = 0
    last_check: float = 0.0
    last_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    total_checks: int = 0
    total_failures: int = 0
    history: list[CheckResult] = field(default_factory=list)

    @property
    def availability_pct(self) -> float:
        if self.total_checks == 0:
            return 100.0
        return (1 - self.total_failures / self.total_checks) * 100

    def to_dict(self) -> dict:
        return {
            "name": self.endpoint.name,
            "model": self.endpoint.model,
            "health": self.health.value,
            "availability_pct": round(self.availability_pct, 2),
            "last_latency_ms": round(self.last_latency_ms, 1),
            "p95_latency_ms": round(self.p95_latency_ms, 1),
            "consecutive_failures": self.consecutive_failures,
            "total_checks": self.total_checks,
            "last_check": self.last_check,
        }


class ModelHealthChecker:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._endpoints: dict[str, EndpointState] = {}
        self._running = False
        self._alert_callbacks: list[Callable] = []
        self._stats: dict[str, int] = {
            "checks": 0,
            "healthy": 0,
            "degraded": 0,
            "unhealthy": 0,
            "alerts": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def add_endpoint(self, endpoint: ModelEndpoint):
        self._endpoints[endpoint.name] = EndpointState(endpoint=endpoint)

    def on_alert(self, callback: Callable):
        self._alert_callbacks.append(callback)

    async def _probe(self, state: EndpointState) -> CheckResult:
        cfg = state.endpoint.check_config
        ep = state.endpoint
        t0 = time.time()

        try:
            messages = [{"role": "user", "content": cfg.canary_prompt}]
            payload = {
                "model": ep.model,
                "messages": messages,
                "max_tokens": 20,
                "temperature": 0,
            }
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=cfg.timeout_s)
            ) as sess:
                async with sess.post(
                    f"{ep.url}/v1/chat/completions", json=payload
                ) as r:
                    lat = (time.time() - t0) * 1000
                    if r.status == 200:
                        data = await r.json(content_type=None)
                        content = data["choices"][0]["message"]["content"]
                        ok = cfg.expected_content.lower() in content.lower()

                        # Determine health based on latency
                        if not ok:
                            health = ModelHealth.DEGRADED
                        elif lat >= cfg.latency_crit_ms:
                            health = ModelHealth.DEGRADED
                        elif lat >= cfg.latency_warn_ms:
                            health = ModelHealth.DEGRADED
                        else:
                            health = ModelHealth.HEALTHY

                        return CheckResult(
                            ep.name,
                            ep.model,
                            ok,
                            lat,
                            health,
                            response_snippet=content[:40],
                        )
                    else:
                        lat = (time.time() - t0) * 1000
                        return CheckResult(
                            ep.name,
                            ep.model,
                            False,
                            lat,
                            ModelHealth.UNHEALTHY,
                            error=f"HTTP {r.status}",
                        )
        except asyncio.TimeoutError:
            return CheckResult(
                ep.name,
                ep.model,
                False,
                cfg.timeout_s * 1000,
                ModelHealth.OFFLINE,
                error="timeout",
            )
        except Exception as e:
            return CheckResult(
                ep.name,
                ep.model,
                False,
                (time.time() - t0) * 1000,
                ModelHealth.OFFLINE,
                error=str(e)[:80],
            )

    def _update_state(self, state: EndpointState, result: CheckResult):
        cfg = state.endpoint.check_config
        prev_health = state.health
        state.total_checks += 1
        state.last_check = result.ts
        state.last_latency_ms = result.latency_ms

        # Update latency P95 (approximate with simple sorted buffer)
        state.history.append(result)
        if len(state.history) > 100:
            state.history.pop(0)
        if state.history:
            lats = sorted(h.latency_ms for h in state.history)
            idx = min(int(len(lats) * 0.95), len(lats) - 1)
            state.p95_latency_ms = lats[idx]

        if result.success:
            state.consecutive_successes += 1
            state.consecutive_failures = 0
            if state.consecutive_successes >= cfg.healthy_threshold:
                state.health = result.health  # HEALTHY or DEGRADED
        else:
            state.consecutive_failures += 1
            state.consecutive_successes = 0
            state.total_failures += 1
            if state.consecutive_failures >= cfg.unhealthy_threshold:
                state.health = (
                    ModelHealth.UNHEALTHY
                    if result.health == ModelHealth.OFFLINE
                    else ModelHealth.UNHEALTHY
                )

        # Alert on health change
        if state.health != prev_health:
            self._stats["alerts"] += 1
            for cb in self._alert_callbacks:
                try:
                    cb(state, prev_health)
                except Exception:
                    pass

        # Update stats
        if state.health == ModelHealth.HEALTHY:
            self._stats["healthy"] += 1
        elif state.health == ModelHealth.DEGRADED:
            self._stats["degraded"] += 1
        elif state.health in (ModelHealth.UNHEALTHY, ModelHealth.OFFLINE):
            self._stats["unhealthy"] += 1

    async def check_one(self, name: str) -> CheckResult | None:
        state = self._endpoints.get(name)
        if not state:
            return None
        self._stats["checks"] += 1
        result = await self._probe(state)
        self._update_state(state, result)

        if self.redis:
            asyncio.create_task(
                self.redis.setex(
                    f"{REDIS_PREFIX}{name}",
                    300,
                    json.dumps(state.to_dict()),
                )
            )
        return result

    async def check_all(self) -> list[CheckResult]:
        tasks = [self.check_one(name) for name in self._endpoints]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return [r for r in results if isinstance(r, CheckResult)]

    async def run(self):
        """Continuous health check loop."""
        self._running = True
        while self._running:
            await self.check_all()
            # Use the minimum interval across all endpoints
            min_interval = min(
                (s.endpoint.check_config.interval_s for s in self._endpoints.values()),
                default=30.0,
            )
            await asyncio.sleep(min_interval)

    def stop(self):
        self._running = False

    def get_health(self, name: str) -> ModelHealth:
        state = self._endpoints.get(name)
        return state.health if state else ModelHealth.UNKNOWN

    def healthy_endpoints(self) -> list[str]:
        return [
            name
            for name, s in self._endpoints.items()
            if s.health == ModelHealth.HEALTHY
        ]

    def all_states(self) -> list[dict]:
        return [s.to_dict() for s in self._endpoints.values()]

    def stats(self) -> dict:
        return {**self._stats, "endpoints": len(self._endpoints)}


def build_jarvis_health_checker() -> ModelHealthChecker:
    checker = ModelHealthChecker()
    fast_cfg = HealthCheckConfig(
        interval_s=15, timeout_s=5, latency_warn_ms=1000, latency_crit_ms=3000
    )
    slow_cfg = HealthCheckConfig(
        interval_s=30, timeout_s=10, latency_warn_ms=3000, latency_crit_ms=8000
    )

    checker.add_endpoint(
        ModelEndpoint(
            "m1-qwen9b",
            "http://192.168.1.85:1234",
            "qwen3.5-9b",
            weight=1.2,
            check_config=fast_cfg,
        )
    )
    checker.add_endpoint(
        ModelEndpoint(
            "m1-qwen27b",
            "http://192.168.1.85:1234",
            "qwen3.5-27b",
            weight=1.0,
            check_config=slow_cfg,
        )
    )
    checker.add_endpoint(
        ModelEndpoint(
            "m2-deepseek",
            "http://192.168.1.26:1234",
            "deepseek-r1-0528",
            weight=0.9,
            check_config=slow_cfg,
        )
    )
    checker.add_endpoint(
        ModelEndpoint(
            "ol1-gemma",
            "http://127.0.0.1:11434",
            "gemma3:4b",
            weight=0.5,
            check_config=fast_cfg,
        )
    )
    return checker


async def main():
    import sys

    checker = build_jarvis_health_checker()
    await checker.connect_redis()

    def on_alert(state: EndpointState, prev: ModelHealth):
        print(f"  ⚠️  {state.endpoint.name}: {prev.value} → {state.health.value}")

    checker.on_alert(on_alert)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Running health checks...")
        results = await checker.check_all()
        print("\nResults:")
        for r in results:
            icon = {
                "healthy": "✅",
                "degraded": "⚠️",
                "unhealthy": "❌",
                "offline": "💀",
                "unknown": "?",
            }.get(r.health.value, "?")
            print(
                f"  {icon} {r.endpoint:<20} {r.health.value:<12} {r.latency_ms:.0f}ms  {r.error or r.response_snippet[:30]}"
            )

        print(f"\nHealthy: {checker.healthy_endpoints()}")
        print(f"Stats: {json.dumps(checker.stats(), indent=2)}")

    elif cmd == "watch":
        print("Starting continuous health monitoring (Ctrl+C to stop)...")
        checker.on_alert(
            lambda s, p: print(f"ALERT: {s.endpoint.name} {p.value}→{s.health.value}")
        )
        try:
            await checker.run()
        except KeyboardInterrupt:
            checker.stop()

    elif cmd == "stats":
        print(json.dumps(checker.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
