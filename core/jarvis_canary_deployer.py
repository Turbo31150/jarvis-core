#!/usr/bin/env python3
"""
jarvis_canary_deployer — Canary deployment manager for JARVIS services
Gradual traffic shifting, automatic rollback on error rate spike, health validation
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.canary_deployer")

REDIS_PREFIX = "jarvis:canary:"


@dataclass
class CanaryTarget:
    name: str
    url: str
    weight: float = 0.0  # 0.0-1.0 traffic fraction
    healthy: bool = True
    requests: int = 0
    errors: int = 0
    latency_sum_ms: float = 0.0

    @property
    def error_rate(self) -> float:
        if not self.requests:
            return 0.0
        return self.errors / self.requests

    @property
    def avg_latency_ms(self) -> float:
        if not self.requests:
            return 0.0
        return round(self.latency_sum_ms / self.requests, 1)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "url": self.url,
            "weight": round(self.weight, 3),
            "healthy": self.healthy,
            "requests": self.requests,
            "errors": self.errors,
            "error_rate": round(self.error_rate, 4),
            "avg_latency_ms": self.avg_latency_ms,
        }


@dataclass
class CanaryDeployment:
    deployment_id: str
    service: str
    stable: CanaryTarget
    canary: CanaryTarget
    status: str = "running"  # running | promoted | rolled_back | paused
    started_at: float = field(default_factory=time.time)
    error_threshold: float = 0.05  # 5% errors triggers rollback
    latency_threshold_ms: float = 2000.0
    step_pct: float = 10.0  # traffic increment per step
    step_interval_s: float = 60.0  # seconds between steps

    def to_dict(self) -> dict:
        return {
            "deployment_id": self.deployment_id,
            "service": self.service,
            "status": self.status,
            "started_at": self.started_at,
            "stable": self.stable.to_dict(),
            "canary": self.canary.to_dict(),
            "config": {
                "error_threshold": self.error_threshold,
                "latency_threshold_ms": self.latency_threshold_ms,
                "step_pct": self.step_pct,
                "step_interval_s": self.step_interval_s,
            },
        }


class CanaryDeployer:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._deployments: dict[str, CanaryDeployment] = {}

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def start_deployment(
        self,
        service: str,
        stable_url: str,
        canary_url: str,
        error_threshold: float = 0.05,
        step_pct: float = 10.0,
        step_interval_s: float = 60.0,
    ) -> CanaryDeployment:
        import uuid

        dep = CanaryDeployment(
            deployment_id=str(uuid.uuid4())[:8],
            service=service,
            stable=CanaryTarget(name="stable", url=stable_url, weight=1.0),
            canary=CanaryTarget(name="canary", url=canary_url, weight=0.0),
            error_threshold=error_threshold,
            step_pct=step_pct,
            step_interval_s=step_interval_s,
        )
        self._deployments[dep.deployment_id] = dep
        log.info(f"Canary deployment [{dep.deployment_id}] started for {service}")
        return dep

    def _select_target(self, dep: CanaryDeployment) -> CanaryTarget:
        """Select target based on weights."""
        import random

        if random.random() < dep.canary.weight:
            return dep.canary
        return dep.stable

    async def route(
        self,
        deployment_id: str,
        path: str = "/health",
        method: str = "GET",
        body: dict | None = None,
    ) -> dict:
        dep = self._deployments.get(deployment_id)
        if not dep or dep.status != "running":
            return {"error": "deployment not active"}

        target = self._select_target(dep)
        t0 = time.time()
        error = False

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            ) as sess:
                url = f"{target.url}{path}"
                if method == "POST":
                    async with sess.post(url, json=body or {}) as r:
                        latency_ms = (time.time() - t0) * 1000
                        result = {"status": r.status, "target": target.name}
                        if r.status >= 500:
                            error = True
                else:
                    async with sess.get(url) as r:
                        latency_ms = (time.time() - t0) * 1000
                        result = {"status": r.status, "target": target.name}
                        if r.status >= 500:
                            error = True
        except Exception as e:
            latency_ms = (time.time() - t0) * 1000
            error = True
            result = {"error": str(e), "target": target.name}

        target.requests += 1
        target.latency_sum_ms += latency_ms
        if error:
            target.errors += 1

        # Check thresholds
        await self._check_health(dep, target, latency_ms)
        return result

    async def _check_health(
        self, dep: CanaryDeployment, target: CanaryTarget, latency_ms: float
    ):
        if target.name != "canary":
            return
        if target.requests < 10:
            return  # Not enough data

        should_rollback = False
        reason = ""

        if target.error_rate > dep.error_threshold:
            should_rollback = True
            reason = f"error rate {target.error_rate:.1%} > threshold {dep.error_threshold:.1%}"
        elif target.avg_latency_ms > dep.latency_threshold_ms:
            should_rollback = True
            reason = f"latency {target.avg_latency_ms:.0f}ms > threshold {dep.latency_threshold_ms:.0f}ms"

        if should_rollback:
            await self.rollback(dep.deployment_id, reason)

    async def step(self, deployment_id: str) -> bool:
        """Increase canary traffic by step_pct. Returns False if fully promoted."""
        dep = self._deployments.get(deployment_id)
        if not dep or dep.status != "running":
            return False

        new_weight = min(1.0, dep.canary.weight + dep.step_pct / 100)
        dep.canary.weight = round(new_weight, 3)
        dep.stable.weight = round(1.0 - new_weight, 3)

        log.info(
            f"Step [{deployment_id}]: canary={dep.canary.weight:.0%} stable={dep.stable.weight:.0%}"
        )

        if dep.canary.weight >= 1.0:
            await self.promote(deployment_id)
            return False

        await self._persist(dep)
        return True

    async def promote(self, deployment_id: str):
        dep = self._deployments.get(deployment_id)
        if not dep:
            return
        dep.status = "promoted"
        dep.canary.weight = 1.0
        dep.stable.weight = 0.0
        log.info(f"✅ Promoted [{deployment_id}] {dep.service} canary → stable")
        await self._persist(dep)

    async def rollback(self, deployment_id: str, reason: str = ""):
        dep = self._deployments.get(deployment_id)
        if not dep:
            return
        dep.status = "rolled_back"
        dep.canary.weight = 0.0
        dep.stable.weight = 1.0
        dep.canary.healthy = False
        log.warning(f"🔴 Rolled back [{deployment_id}] {dep.service}: {reason}")
        await self._persist(dep)

    async def auto_step_loop(self, deployment_id: str):
        """Automatically step traffic every step_interval_s."""
        dep = self._deployments.get(deployment_id)
        if not dep:
            return
        while dep.status == "running":
            await asyncio.sleep(dep.step_interval_s)
            if dep.status != "running":
                break
            more = await self.step(deployment_id)
            if not more:
                break

    async def _persist(self, dep: CanaryDeployment):
        if self.redis:
            await self.redis.set(
                f"{REDIS_PREFIX}{dep.deployment_id}",
                json.dumps(dep.to_dict()),
                ex=86400,
            )

    def get(self, deployment_id: str) -> CanaryDeployment | None:
        return self._deployments.get(deployment_id)

    def list_all(self) -> list[dict]:
        return [d.to_dict() for d in self._deployments.values()]


async def main():
    import sys

    deployer = CanaryDeployer()
    await deployer.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        dep = deployer.start_deployment(
            service="inference_api",
            stable_url="http://127.0.0.1:1234",
            canary_url="http://192.168.1.26:1234",
            step_pct=20.0,
            step_interval_s=5.0,
        )
        print(f"Deployment [{dep.deployment_id}] started")
        print(f"  stable={dep.stable.weight:.0%} canary={dep.canary.weight:.0%}")

        # Simulate stepping
        for i in range(6):
            more = await deployer.step(dep.deployment_id)
            d = deployer.get(dep.deployment_id)
            print(f"  Step {i + 1}: canary={d.canary.weight:.0%} status={d.status}")
            if not more:
                break

    elif cmd == "list":
        for d in deployer.list_all():
            print(
                f"  [{d['deployment_id']}] {d['service']} status={d['status']} canary={d['canary']['weight']:.0%}"
            )

    elif cmd == "rollback" and len(sys.argv) > 2:
        await deployer.rollback(sys.argv[2], "manual")
        print(f"Rolled back {sys.argv[2]}")


if __name__ == "__main__":
    asyncio.run(main())
