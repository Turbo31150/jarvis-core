#!/usr/bin/env python3
"""
jarvis_api_mock — Configurable HTTP mock server for LLM endpoint testing
Records requests, replays fixtures, simulates latency and errors
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from aiohttp import web

log = logging.getLogger("jarvis.api_mock")

FIXTURES_FILE = Path("/home/turbo/IA/Core/jarvis/data/mock_fixtures.jsonl")


class MockBehavior(str, Enum):
    PASS = "pass"  # return fixture or default response
    ERROR = "error"  # return HTTP error code
    TIMEOUT = "timeout"  # hang until client times out
    SLOW = "slow"  # add artificial latency
    FLAKY = "flaky"  # randomly fail a % of requests


@dataclass
class MockRoute:
    path: str  # e.g. "/v1/chat/completions"
    method: str = "POST"
    behavior: MockBehavior = MockBehavior.PASS
    status_code: int = 200
    response_body: dict | None = None
    latency_ms: float = 0.0
    error_rate: float = 0.0  # 0.0–1.0 for FLAKY
    error_code: int = 500
    call_count: int = 0

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "method": self.method,
            "behavior": self.behavior.value,
            "status_code": self.status_code,
            "latency_ms": self.latency_ms,
            "call_count": self.call_count,
        }


@dataclass
class RecordedRequest:
    req_id: str
    path: str
    method: str
    headers: dict[str, str]
    body: Any
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "req_id": self.req_id,
            "path": self.path,
            "method": self.method,
            "body_preview": str(self.body)[:200] if self.body else None,
            "ts": self.ts,
        }


def _default_chat_response(body: dict) -> dict:
    model = body.get("model", "mock-model")
    messages = body.get("messages", [])
    last = messages[-1]["content"] if messages else "Hello"
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": f"[MOCK] Echo: {last[:80]}",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }


def _default_embed_response(body: dict) -> dict:
    inputs = body.get("input", [])
    if isinstance(inputs, str):
        inputs = [inputs]
    return {
        "object": "list",
        "model": body.get("model", "mock-embed"),
        "data": [
            {"object": "embedding", "index": i, "embedding": [0.1] * 128}
            for i in range(len(inputs))
        ],
        "usage": {"prompt_tokens": len(inputs) * 5, "total_tokens": len(inputs) * 5},
    }


def _default_models_response() -> dict:
    return {
        "object": "list",
        "data": [
            {"id": "mock-model", "object": "model", "owned_by": "jarvis"},
            {"id": "mock-embed", "object": "model", "owned_by": "jarvis"},
        ],
    }


class ApiMock:
    def __init__(self, host: str = "127.0.0.1", port: int = 9999):
        self._host = host
        self._port = port
        self._routes: dict[str, MockRoute] = {}
        self._recorded: list[RecordedRequest] = []
        self._app = web.Application()
        self._runner: web.AppRunner | None = None
        self._stats: dict[str, int] = {
            "total_requests": 0,
            "passed": 0,
            "errors": 0,
            "timeouts": 0,
        }
        self._setup_default_routes()
        self._app.router.add_route("*", "/{path_info:.*}", self._handle)

    def _setup_default_routes(self):
        self.add_route(MockRoute("/v1/chat/completions", latency_ms=50))
        self.add_route(MockRoute("/v1/embeddings", latency_ms=20))
        self.add_route(MockRoute("/v1/models", method="GET"))

    def add_route(self, route: MockRoute):
        key = f"{route.method.upper()}:{route.path}"
        self._routes[key] = route

    def set_behavior(self, path: str, behavior: MockBehavior, **kwargs):
        for key, route in self._routes.items():
            if route.path == path:
                route.behavior = behavior
                for k, v in kwargs.items():
                    if hasattr(route, k):
                        setattr(route, k, v)

    async def _handle(self, request: web.Request) -> web.Response:
        import random

        path = "/" + request.match_info.get("path_info", "")
        method = request.method.upper()
        key = f"{method}:{path}"
        route = self._routes.get(key)

        # Try method-agnostic match
        if not route:
            for rkey, r in self._routes.items():
                if r.path == path:
                    route = r
                    break

        self._stats["total_requests"] += 1

        # Record request
        try:
            body = await request.json()
        except Exception:
            body = None

        self._recorded.append(
            RecordedRequest(
                req_id=str(uuid.uuid4())[:8],
                path=path,
                method=method,
                headers=dict(request.headers),
                body=body,
            )
        )
        if len(self._recorded) > 500:
            self._recorded.pop(0)

        if not route:
            return web.json_response(
                {"error": f"No mock for {method} {path}"}, status=404
            )

        route.call_count += 1

        # Behavior dispatch
        if route.behavior == MockBehavior.TIMEOUT:
            self._stats["timeouts"] += 1
            await asyncio.sleep(60)
            return web.json_response({}, status=408)

        if route.behavior == MockBehavior.ERROR:
            self._stats["errors"] += 1
            return web.json_response({"error": "mock error"}, status=route.error_code)

        if route.behavior == MockBehavior.FLAKY:
            if random.random() < route.error_rate:
                self._stats["errors"] += 1
                return web.json_response(
                    {"error": "flaky error"}, status=route.error_code
                )

        if route.latency_ms > 0 or route.behavior == MockBehavior.SLOW:
            await asyncio.sleep(route.latency_ms / 1000)

        # Build response
        if route.response_body:
            resp_body = route.response_body
        elif path == "/v1/chat/completions":
            resp_body = _default_chat_response(body or {})
        elif path == "/v1/embeddings":
            resp_body = _default_embed_response(body or {})
        elif path == "/v1/models":
            resp_body = _default_models_response()
        else:
            resp_body = {"ok": True, "path": path}

        self._stats["passed"] += 1
        return web.json_response(resp_body, status=route.status_code)

    async def start(self):
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        log.info(f"ApiMock running on http://{self._host}:{self._port}")

    async def stop(self):
        if self._runner:
            await self._runner.cleanup()

    def recorded_requests(self, path: str | None = None, n: int = 20) -> list[dict]:
        recs = self._recorded
        if path:
            recs = [r for r in recs if r.path == path]
        return [r.to_dict() for r in recs[-n:]]

    def route_stats(self) -> list[dict]:
        return [r.to_dict() for r in self._routes.values()]

    def reset(self):
        self._recorded.clear()
        for r in self._routes.values():
            r.call_count = 0
        self._stats = {k: 0 for k in self._stats}

    def stats(self) -> dict:
        return {
            **self._stats,
            "routes": len(self._routes),
            "recorded": len(self._recorded),
        }

    def save_fixtures(self):
        FIXTURES_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(FIXTURES_FILE, "w") as f:
            for rec in self._recorded:
                f.write(json.dumps(rec.to_dict()) + "\n")


def build_jarvis_api_mock(port: int = 9999) -> "ApiMock":
    mock = ApiMock(port=port)
    mock.add_route(
        MockRoute(
            "/v1/chat/completions",
            latency_ms=80,
            behavior=MockBehavior.PASS,
        )
    )
    return mock


async def main():
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"

    if cmd == "serve":
        mock = build_jarvis_api_mock(port=9999)
        await mock.start()
        print("Mock API running on http://127.0.0.1:9999")
        print("Endpoints: /v1/chat/completions  /v1/embeddings  /v1/models")
        try:
            await asyncio.sleep(3600)
        except KeyboardInterrupt:
            pass
        await mock.stop()

    elif cmd == "demo":
        import aiohttp

        mock = build_jarvis_api_mock(port=9999)
        await mock.start()
        await asyncio.sleep(0.1)

        async with aiohttp.ClientSession() as sess:
            # Test chat
            async with sess.post(
                "http://127.0.0.1:9999/v1/chat/completions",
                json={
                    "model": "qwen3.5",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
            ) as r:
                data = await r.json()
                print(f"Chat: {data['choices'][0]['message']['content']}")

            # Test embeddings
            async with sess.post(
                "http://127.0.0.1:9999/v1/embeddings",
                json={"model": "nomic", "input": ["hello", "world"]},
            ) as r:
                data = await r.json()
                print(
                    f"Embed: {len(data['data'])} vectors dim={len(data['data'][0]['embedding'])}"
                )

            # Test models
            async with sess.get("http://127.0.0.1:9999/v1/models") as r:
                data = await r.json()
                print(f"Models: {[m['id'] for m in data['data']]}")

        print(f"\nStats: {json.dumps(mock.stats(), indent=2)}")
        print(f"Recorded: {mock.recorded_requests()}")
        await mock.stop()


if __name__ == "__main__":
    asyncio.run(main())
