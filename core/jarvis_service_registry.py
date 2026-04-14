#!/usr/bin/env python3
"""
jarvis_service_registry — Service discovery and health tracking
Register, discover, and health-check microservices and agents in the JARVIS cluster
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.service_registry")

REDIS_PREFIX = "jarvis:svc:"
HEARTBEAT_TTL_S = 30
HEALTH_CHECK_INTERVAL_S = 15


class ServiceStatus(str, Enum):
    UP = "up"
    DOWN = "down"
    DEGRADED = "degraded"
    STARTING = "starting"
    UNKNOWN = "unknown"


@dataclass
class ServiceEndpoint:
    host: str
    port: int
    path: str = "/"
    protocol: str = "http"

    @property
    def url(self) -> str:
        return f"{self.protocol}://{self.host}:{self.port}{self.path}"


@dataclass
class ServiceRecord:
    service_id: str
    name: str
    version: str
    node: str
    endpoints: list[ServiceEndpoint]
    tags: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    status: ServiceStatus = ServiceStatus.STARTING
    health_url: str = ""
    registered_at: float = field(default_factory=time.time)
    last_heartbeat: float = field(default_factory=time.time)
    check_failures: int = 0

    @property
    def primary_url(self) -> str:
        return self.endpoints[0].url if self.endpoints else ""

    @property
    def age_s(self) -> float:
        return time.time() - self.last_heartbeat

    @property
    def healthy(self) -> bool:
        return self.status == ServiceStatus.UP and self.age_s < HEARTBEAT_TTL_S

    def to_dict(self) -> dict:
        return {
            "service_id": self.service_id,
            "name": self.name,
            "version": self.version,
            "node": self.node,
            "endpoints": [{"url": e.url} for e in self.endpoints],
            "tags": self.tags,
            "metadata": self.metadata,
            "status": self.status.value,
            "primary_url": self.primary_url,
            "registered_at": self.registered_at,
            "last_heartbeat": self.last_heartbeat,
            "age_s": round(self.age_s, 1),
            "healthy": self.healthy,
        }


class ServiceRegistry:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._services: dict[str, ServiceRecord] = {}
        self._health_task: asyncio.Task | None = None

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
            await self._load_from_redis()
        except Exception:
            self.redis = None

    async def _load_from_redis(self):
        if not self.redis:
            return
        raw = await self.redis.hgetall(f"{REDIS_PREFIX}services")
        for sid, data_str in raw.items():
            try:
                d = json.loads(data_str)
                endpoints = [
                    ServiceEndpoint(
                        host=e.get("host", ""),
                        port=e.get("port", 80),
                        path=e.get("path", "/"),
                    )
                    for e in d.get("endpoint_list", [])
                ]
                rec = ServiceRecord(
                    service_id=d["service_id"],
                    name=d["name"],
                    version=d.get("version", "1.0"),
                    node=d.get("node", "M1"),
                    endpoints=endpoints,
                    tags=d.get("tags", []),
                    metadata=d.get("metadata", {}),
                    status=ServiceStatus(d.get("status", "unknown")),
                    last_heartbeat=d.get("last_heartbeat", 0),
                )
                self._services[sid] = rec
            except Exception:
                pass

    async def register(
        self,
        name: str,
        version: str,
        node: str,
        host: str,
        port: int,
        path: str = "/",
        health_path: str = "/health",
        tags: list[str] | None = None,
        metadata: dict | None = None,
        service_id: str | None = None,
    ) -> ServiceRecord:
        sid = service_id or f"{name}-{str(uuid.uuid4())[:6]}"
        endpoint = ServiceEndpoint(host=host, port=port, path=path)
        rec = ServiceRecord(
            service_id=sid,
            name=name,
            version=version,
            node=node,
            endpoints=[endpoint],
            tags=tags or [],
            metadata=metadata or {},
            status=ServiceStatus.STARTING,
            health_url=f"http://{host}:{port}{health_path}",
        )
        self._services[sid] = rec

        if self.redis:
            d = rec.to_dict()
            d["endpoint_list"] = [
                {"host": e.host, "port": e.port, "path": e.path} for e in rec.endpoints
            ]
            await self.redis.hset(f"{REDIS_PREFIX}services", sid, json.dumps(d))
            await self.redis.sadd(f"{REDIS_PREFIX}names:{name}", sid)
            for tag in rec.tags:
                await self.redis.sadd(f"{REDIS_PREFIX}tags:{tag}", sid)

        log.info(f"Service registered: {name} [{sid}] at {endpoint.url}")
        return rec

    async def deregister(self, service_id: str):
        rec = self._services.pop(service_id, None)
        if rec and self.redis:
            await self.redis.hdel(f"{REDIS_PREFIX}services", service_id)
            await self.redis.srem(f"{REDIS_PREFIX}names:{rec.name}", service_id)
        if rec:
            log.info(f"Service deregistered: {rec.name} [{service_id}]")

    async def heartbeat(
        self,
        service_id: str,
        status: ServiceStatus = ServiceStatus.UP,
        metadata: dict | None = None,
    ):
        rec = self._services.get(service_id)
        if not rec:
            return
        rec.last_heartbeat = time.time()
        rec.status = status
        if metadata:
            rec.metadata.update(metadata)
        if self.redis:
            await self.redis.setex(
                f"{REDIS_PREFIX}hb:{service_id}", HEARTBEAT_TTL_S * 2, str(time.time())
            )

    def discover(
        self,
        name: str | None = None,
        tag: str | None = None,
        node: str | None = None,
        healthy_only: bool = True,
    ) -> list[ServiceRecord]:
        results = list(self._services.values())
        if healthy_only:
            results = [s for s in results if s.healthy]
        if name:
            results = [s for s in results if s.name == name]
        if tag:
            results = [s for s in results if tag in s.tags]
        if node:
            results = [s for s in results if s.node == node]
        return sorted(results, key=lambda s: s.check_failures)

    def get(self, service_id: str) -> ServiceRecord | None:
        return self._services.get(service_id)

    def best(self, name: str) -> ServiceRecord | None:
        candidates = self.discover(name=name, healthy_only=True)
        return candidates[0] if candidates else None

    async def _check_health(self, rec: ServiceRecord):
        if not rec.health_url:
            return
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5)
            ) as sess:
                async with sess.get(rec.health_url) as r:
                    if r.status == 200:
                        rec.status = ServiceStatus.UP
                        rec.check_failures = 0
                        rec.last_heartbeat = time.time()
                    else:
                        rec.check_failures += 1
                        rec.status = (
                            ServiceStatus.DEGRADED
                            if rec.check_failures < 3
                            else ServiceStatus.DOWN
                        )
        except Exception:
            rec.check_failures += 1
            rec.status = (
                ServiceStatus.DOWN
                if rec.check_failures >= 3
                else ServiceStatus.DEGRADED
            )

    async def start_health_checks(self):
        async def loop():
            while True:
                for rec in list(self._services.values()):
                    await self._check_health(rec)
                await asyncio.sleep(HEALTH_CHECK_INTERVAL_S)

        self._health_task = asyncio.create_task(loop())

    def stats(self) -> dict:
        svcs = list(self._services.values())
        return {
            "total": len(svcs),
            "healthy": sum(1 for s in svcs if s.healthy),
            "down": sum(1 for s in svcs if s.status == ServiceStatus.DOWN),
            "names": list({s.name for s in svcs}),
            "nodes": list({s.node for s in svcs}),
        }


async def main():
    import sys

    registry = ServiceRegistry()
    await registry.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        # Register JARVIS services
        await registry.register(
            "jarvis-api", "2.0", "M1", "127.0.0.1", 8768, tags=["api", "core"]
        )
        await registry.register(
            "jarvis-llm", "1.0", "M1", "127.0.0.1", 1234, tags=["llm", "inference"]
        )
        await registry.register(
            "jarvis-llm", "1.0", "M2", "192.168.1.26", 1234, tags=["llm", "inference"]
        )
        await registry.register(
            "jarvis-embed",
            "1.0",
            "M2",
            "192.168.1.26",
            1234,
            path="/v1/embeddings",
            tags=["embedding"],
        )
        await registry.register(
            "jarvis-ws", "1.0", "M1", "127.0.0.1", 8769, tags=["websocket", "streaming"]
        )

        # Simulate heartbeats
        for sid, rec in list(registry._services.items())[:3]:
            await registry.heartbeat(sid, ServiceStatus.UP)

        # Discover
        llm_services = registry.discover(name="jarvis-llm")
        print(f"LLM services: {len(llm_services)}")
        for s in llm_services:
            print(f"  {s.service_id} @ {s.primary_url} [{s.status.value}]")

        print(f"\nStats: {json.dumps(registry.stats(), indent=2)}")

    elif cmd == "list":
        svcs = registry.discover(healthy_only=False)
        for s in svcs:
            icon = "✅" if s.healthy else "❌"
            print(f"  {icon} {s.name:<20} {s.version:<8} {s.node:<5} {s.primary_url}")

    elif cmd == "stats":
        print(json.dumps(registry.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
