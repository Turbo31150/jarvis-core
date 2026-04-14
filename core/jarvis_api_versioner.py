#!/usr/bin/env python3
"""
jarvis_api_versioner — API version management and routing
Handles v1/v2/v3 endpoint versioning, deprecation notices, migration helpers
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Callable

import aiohttp
import redis.asyncio as aioredis
from aiohttp import web

log = logging.getLogger("jarvis.api_versioner")

API_PORT = 8770
REDIS_KEY = "jarvis:api:versions"


@dataclass
class APIVersion:
    version: str  # "v1", "v2", "v3"
    status: str  # active | deprecated | sunset
    introduced: str  # "2025-01-01"
    deprecated: str = ""  # date when deprecated
    sunset: str = ""  # date when removed
    base_url: str = ""  # upstream to proxy to
    changelog: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "status": self.status,
            "introduced": self.introduced,
            "deprecated": self.deprecated,
            "sunset": self.sunset,
            "base_url": self.base_url,
            "changelog": self.changelog,
        }


@dataclass
class RouteMapping:
    version: str
    path: str  # e.g. "/chat/completions"
    method: str  # GET | POST | *
    handler: Callable | None = None
    proxy_url: str = ""  # if no handler, proxy here
    deprecated: bool = False
    replacement: str = ""  # newer path


class APIVersioner:
    def __init__(self, port: int = API_PORT):
        self.port = port
        self.redis: aioredis.Redis | None = None
        self._versions: dict[str, APIVersion] = {}
        self._routes: list[RouteMapping] = []
        self._request_counts: dict[str, int] = {}  # "v1:/path" → count
        self._register_defaults()

    def _register_defaults(self):
        self._versions["v1"] = APIVersion(
            version="v1",
            status="deprecated",
            introduced="2025-01-01",
            deprecated="2025-06-01",
            sunset="2026-12-01",
            base_url="http://127.0.0.1:1234",
            changelog=["Initial release"],
        )
        self._versions["v2"] = APIVersion(
            version="v2",
            status="active",
            introduced="2025-06-01",
            base_url="http://127.0.0.1:1234",
            changelog=["Streaming support", "Multi-model routing", "Tool calls"],
        )
        self._versions["v3"] = APIVersion(
            version="v3",
            status="active",
            introduced="2026-01-01",
            base_url="http://127.0.0.1:1234",
            changelog=["RAG injection", "Shadow mode", "Canary routing", "Metrics"],
        )

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def register_route(self, route: RouteMapping):
        self._routes.append(route)

    def _detect_version(self, request: web.Request) -> str:
        # 1. Path prefix: /v2/chat → "v2"
        path = request.path
        for v in sorted(self._versions.keys(), reverse=True):
            if path.startswith(f"/{v}/"):
                return v
        # 2. Header: X-API-Version: v2
        header = request.headers.get("X-API-Version", "")
        if header in self._versions:
            return header
        # 3. Query param: ?api_version=v2
        qp = request.rel_url.query.get("api_version", "")
        if qp in self._versions:
            return qp
        # Default to latest active
        active = [v for v, info in self._versions.items() if info.status == "active"]
        return sorted(active)[-1] if active else "v3"

    def _build_deprecation_headers(self, version: str) -> dict:
        info = self._versions.get(version)
        if not info or info.status == "active":
            return {}
        headers = {
            "Deprecation": f'date="{info.deprecated}"',
            "X-API-Version-Status": info.status,
        }
        if info.sunset:
            headers["Sunset"] = info.sunset
        # Find latest active version
        active = sorted([v for v, i in self._versions.items() if i.status == "active"])
        if active:
            headers["Link"] = f'</{active[-1]}/...>; rel="successor-version"'
        return headers

    async def handle_request(self, request: web.Request) -> web.Response:
        version = self._detect_version(request)
        path = request.path

        # Strip version prefix for actual path
        if path.startswith(f"/{version}/"):
            api_path = path[len(f"/{version}") :]
        else:
            api_path = path

        # Track usage
        key = f"{version}:{api_path}"
        self._request_counts[key] = self._request_counts.get(key, 0) + 1
        if self.redis:
            await self.redis.hincrby("jarvis:api:request_counts", key, 1)

        # Find matching route
        matched_route = None
        for route in self._routes:
            if route.version == version and route.path == api_path:
                if route.method in ("*", request.method):
                    matched_route = route
                    break

        # Build deprecation headers
        dep_headers = self._build_deprecation_headers(version)

        if matched_route:
            if matched_route.handler:
                resp = await matched_route.handler(request)
                for k, v in dep_headers.items():
                    resp.headers[k] = v
                return resp
            elif matched_route.proxy_url:
                return await self._proxy(
                    request, matched_route.proxy_url + api_path, dep_headers
                )

        # Default: proxy to version's base_url
        info = self._versions.get(version)
        if info and info.base_url:
            return await self._proxy(request, info.base_url + api_path, dep_headers)

        return web.Response(
            text=json.dumps(
                {"error": "Route not found", "version": version, "path": api_path}
            ),
            status=404,
            content_type="application/json",
            headers=dep_headers,
        )

    async def _proxy(
        self, request: web.Request, target_url: str, extra_headers: dict
    ) -> web.Response:
        try:
            body = await request.read()
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=120)
            ) as sess:
                method = getattr(sess, request.method.lower())
                async with method(
                    target_url,
                    data=body,
                    headers={
                        k: v
                        for k, v in request.headers.items()
                        if k not in ("Host", "Content-Length")
                    },
                ) as r:
                    resp_body = await r.read()
                    headers = dict(r.headers)
                    headers.update(extra_headers)
                    headers["X-Proxied-By"] = "jarvis-api-versioner"
                    return web.Response(
                        body=resp_body,
                        status=r.status,
                        headers=headers,
                    )
        except Exception as e:
            log.error(f"Proxy error → {target_url}: {e}")
            return web.Response(
                text=json.dumps({"error": str(e)}),
                status=502,
                content_type="application/json",
            )

    async def handle_versions(self, request: web.Request) -> web.Response:
        return web.Response(
            text=json.dumps(
                {
                    "versions": [v.to_dict() for v in self._versions.values()],
                    "current": max(
                        [v for v, i in self._versions.items() if i.status == "active"],
                        default="v3",
                    ),
                },
                indent=2,
            ),
            content_type="application/json",
        )

    async def handle_stats(self, request: web.Request) -> web.Response:
        return web.Response(
            text=json.dumps(
                {
                    "request_counts": self._request_counts,
                    "total": sum(self._request_counts.values()),
                },
                indent=2,
            ),
            content_type="application/json",
        )

    async def run(self):
        app = web.Application()
        app.router.add_get("/_versions", self.handle_versions)
        app.router.add_get("/_stats", self.handle_stats)
        app.router.add_route("*", "/{tail:.*}", self.handle_request)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.port)
        await site.start()
        log.info(f"API versioner running on :{self.port}")
        return runner


async def main():
    import sys

    versioner = APIVersioner()
    await versioner.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "versions"

    if cmd == "serve":
        runner = await versioner.run()
        print(f"API versioner on :{versioner.port} (Ctrl+C to stop)")
        try:
            while True:
                await asyncio.sleep(3600)
        except (KeyboardInterrupt, asyncio.CancelledError):
            await runner.cleanup()

    elif cmd == "versions":
        print(
            f"{'Version':<8} {'Status':<12} {'Introduced':<12} {'Deprecated':<12} {'Sunset'}"
        )
        print("-" * 60)
        for v in versioner._versions.values():
            print(
                f"  {v.version:<8} {v.status:<12} {v.introduced:<12} {v.deprecated or '-':<12} {v.sunset or '-'}"
            )

    elif cmd == "stats":
        total = sum(versioner._request_counts.values())
        print(f"Total requests: {total}")
        for k, c in sorted(versioner._request_counts.items(), key=lambda x: -x[1])[:20]:
            print(f"  {k}: {c}")


if __name__ == "__main__":
    asyncio.run(main())
