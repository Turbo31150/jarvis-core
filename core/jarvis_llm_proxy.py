#!/usr/bin/env python3
"""
jarvis_llm_proxy — Transparent LLM API proxy with request/response interception
Intercepts, logs, modifies, and forwards LLM requests with middleware pipeline
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

import aiohttp
import redis.asyncio as aioredis
from aiohttp import web

log = logging.getLogger("jarvis.llm_proxy")

REDIS_PREFIX = "jarvis:llmproxy:"
DEFAULT_PORT = 8766
DEFAULT_UPSTREAM = "http://127.0.0.1:1234"


@dataclass
class ProxyRequest:
    req_id: str
    method: str
    path: str
    headers: dict
    body: dict
    upstream: str
    ts: float = field(default_factory=time.time)


@dataclass
class ProxyResponse:
    req_id: str
    status: int
    body: dict
    upstream: str
    duration_ms: float
    modified: bool = False


# Middleware type: async fn(req, resp_or_None) -> (req, resp_or_None)
Middleware = Callable[[ProxyRequest, Any | None], tuple[ProxyRequest, Any | None]]


class LLMProxy:
    def __init__(self, port: int = DEFAULT_PORT, upstream: str = DEFAULT_UPSTREAM):
        self.redis: aioredis.Redis | None = None
        self.port = port
        self.upstream = upstream
        self._request_middleware: list[Middleware] = []
        self._response_middleware: list[Middleware] = []
        self._stats: dict[str, Any] = {
            "requests": 0,
            "errors": 0,
            "modified_req": 0,
            "modified_resp": 0,
            "total_tokens_in": 0,
            "total_tokens_out": 0,
        }
        self._history: list[dict] = []
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def add_request_middleware(self, fn: Middleware):
        """fn(req, None) → (req, resp_or_None). Return resp to short-circuit upstream."""
        self._request_middleware.append(fn)

    def add_response_middleware(self, fn: Middleware):
        """fn(req, resp) → (req, resp). Modify response before returning to client."""
        self._response_middleware.append(fn)

    async def _run_request_middleware(
        self, req: ProxyRequest
    ) -> tuple[ProxyRequest, ProxyResponse | None]:
        resp = None
        for mw in self._request_middleware:
            req, resp = await mw(req, resp)
            if resp is not None:
                return req, resp
        return req, None

    async def _run_response_middleware(
        self, req: ProxyRequest, resp: ProxyResponse
    ) -> ProxyResponse:
        for mw in self._response_middleware:
            req, resp = await mw(req, resp)
        return resp

    async def _forward(self, req: ProxyRequest) -> ProxyResponse:
        t0 = time.time()
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=120)
            ) as session:
                async with session.request(
                    req.method,
                    f"{req.upstream}{req.path}",
                    json=req.body if req.body else None,
                    headers={
                        k: v
                        for k, v in req.headers.items()
                        if k.lower() not in ("host", "content-length")
                    },
                ) as r:
                    body = await r.json(content_type=None)
                    return ProxyResponse(
                        req_id=req.req_id,
                        status=r.status,
                        body=body,
                        upstream=req.upstream,
                        duration_ms=(time.time() - t0) * 1000,
                    )
        except Exception as e:
            return ProxyResponse(
                req_id=req.req_id,
                status=502,
                body={"error": str(e)},
                upstream=req.upstream,
                duration_ms=(time.time() - t0) * 1000,
            )

    async def _handle(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = {}

        proxy_req = ProxyRequest(
            req_id=str(uuid.uuid4())[:10],
            method=request.method,
            path=request.path,
            headers=dict(request.headers),
            body=body,
            upstream=self.upstream,
        )

        self._stats["requests"] += 1

        # Request middleware (may short-circuit)
        proxy_req, short_circuit = await self._run_request_middleware(proxy_req)
        if short_circuit is not None:
            resp = short_circuit
        else:
            resp = await self._forward(proxy_req)

        # Response middleware
        resp = await self._run_response_middleware(proxy_req, resp)

        # Track tokens
        usage = resp.body.get("usage", {})
        self._stats["total_tokens_in"] += usage.get("prompt_tokens", 0)
        self._stats["total_tokens_out"] += usage.get("completion_tokens", 0)

        if resp.status >= 500:
            self._stats["errors"] += 1

        entry = {
            "req_id": proxy_req.req_id,
            "path": proxy_req.path,
            "status": resp.status,
            "duration_ms": round(resp.duration_ms, 1),
            "model": body.get("model", ""),
        }
        self._history.append(entry)
        if len(self._history) > 1000:
            self._history = self._history[-1000:]

        if self.redis:
            asyncio.create_task(
                self.redis.lpush(f"{REDIS_PREFIX}history", json.dumps(entry))
            )
            asyncio.create_task(self.redis.ltrim(f"{REDIS_PREFIX}history", 0, 999))

        return web.Response(
            status=resp.status,
            text=json.dumps(resp.body),
            content_type="application/json",
        )

    async def start(self):
        self._app = web.Application()
        self._app.router.add_route("*", "/{path_info:.*}", self._handle)
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self.port)
        await site.start()
        log.info(f"LLMProxy listening on :{self.port} → {self.upstream}")

    async def stop(self):
        if self._runner:
            await self._runner.cleanup()

    def stats(self) -> dict:
        return {
            **self._stats,
            "history_size": len(self._history),
            "request_middleware": len(self._request_middleware),
            "response_middleware": len(self._response_middleware),
        }


def build_logging_proxy(
    port: int = DEFAULT_PORT, upstream: str = DEFAULT_UPSTREAM
) -> LLMProxy:
    """Ready-to-use proxy that logs all requests."""
    proxy = LLMProxy(port=port, upstream=upstream)

    async def log_request(req: ProxyRequest, resp):
        log.info(f"→ {req.method} {req.path} model={req.body.get('model', '?')}")
        return req, resp

    async def log_response(req: ProxyRequest, resp: ProxyResponse):
        log.info(f"← {resp.status} {resp.duration_ms:.0f}ms [{req.req_id}]")
        return req, resp

    proxy.add_request_middleware(log_request)
    proxy.add_response_middleware(log_response)
    return proxy


async def main():
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        proxy = build_logging_proxy()
        await proxy.connect_redis()

        # Add a middleware that injects system context
        async def inject_system(req: ProxyRequest, resp):
            if req.path.endswith("/chat/completions"):
                msgs = req.body.get("messages", [])
                has_system = any(m.get("role") == "system" for m in msgs)
                if not has_system:
                    req.body["messages"] = [
                        {"role": "system", "content": "You are JARVIS."}
                    ] + msgs
                    proxy._stats["modified_req"] += 1
            return req, resp

        proxy.add_request_middleware(inject_system)
        await proxy.start()
        print(f"LLMProxy running on :{DEFAULT_PORT} → {DEFAULT_UPSTREAM}")
        print("Ctrl+C to stop")
        try:
            await asyncio.sleep(3600)
        except KeyboardInterrupt:
            pass
        await proxy.stop()

    elif cmd == "stats":
        proxy = LLMProxy()
        print(json.dumps(proxy.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
