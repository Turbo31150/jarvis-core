#!/usr/bin/env python3
"""
jarvis_webhook_receiver — HTTP webhook ingestion with signature verification
Receives, validates, queues, and dispatches incoming webhook payloads
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import redis.asyncio as aioredis
from aiohttp import web

log = logging.getLogger("jarvis.webhook_receiver")

REDIS_PREFIX = "jarvis:webhook:"
EVENTS_FILE = Path("/home/turbo/IA/Core/jarvis/data/webhook_events.jsonl")
DEFAULT_PORT = 8765


@dataclass
class WebhookEndpoint:
    name: str
    path: str
    secret: str = ""
    signature_header: str = "X-Signature-256"
    allowed_events: list[str] = field(default_factory=list)  # empty = all
    max_payload_kb: int = 256

    def verify_signature(self, body: bytes, signature: str) -> bool:
        if not self.secret:
            return True
        expected = (
            "sha256=" + hmac.new(self.secret.encode(), body, hashlib.sha256).hexdigest()
        )
        return hmac.compare_digest(expected, signature)


@dataclass
class WebhookEvent:
    event_id: str
    endpoint: str
    event_type: str
    payload: dict
    headers: dict
    received_at: float = field(default_factory=time.time)
    processed: bool = False
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "endpoint": self.endpoint,
            "event_type": self.event_type,
            "payload": self.payload,
            "received_at": self.received_at,
            "processed": self.processed,
            "error": self.error,
        }


class WebhookReceiver:
    def __init__(self, port: int = DEFAULT_PORT):
        self.redis: aioredis.Redis | None = None
        self.port = port
        self._endpoints: dict[str, WebhookEndpoint] = {}
        self._handlers: dict[str, list[Callable]] = {}  # endpoint → handlers
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=10000)
        self._events: list[WebhookEvent] = []
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._stats: dict[str, int] = {
            "received": 0,
            "rejected": 0,
            "processed": 0,
            "errors": 0,
        }
        EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def register_endpoint(self, endpoint: WebhookEndpoint):
        self._endpoints[endpoint.path] = endpoint
        log.info(f"Webhook endpoint registered: {endpoint.path} ({endpoint.name})")

    def on_event(self, path: str, handler: Callable):
        """Register async handler(event: WebhookEvent) for endpoint path."""
        if path not in self._handlers:
            self._handlers[path] = []
        self._handlers[path].append(handler)

    async def _handle_request(self, request: web.Request) -> web.Response:
        path = request.path
        endpoint = self._endpoints.get(path)
        if not endpoint:
            return web.Response(status=404, text="Unknown endpoint")

        # Size check
        cl = request.content_length or 0
        if cl > endpoint.max_payload_kb * 1024:
            self._stats["rejected"] += 1
            return web.Response(status=413, text="Payload too large")

        body = await request.read()

        # Signature check
        if endpoint.secret:
            sig = request.headers.get(endpoint.signature_header, "")
            if not endpoint.verify_signature(body, sig):
                self._stats["rejected"] += 1
                log.warning(f"Invalid signature on {path}")
                return web.Response(status=401, text="Invalid signature")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {"raw": body.decode(errors="replace")[:1000]}

        event_type = (
            payload.get("event")
            or payload.get("type")
            or request.headers.get("X-Event-Type", "unknown")
        )

        # Event filter
        if endpoint.allowed_events and event_type not in endpoint.allowed_events:
            return web.Response(status=200, text="Event ignored")

        event = WebhookEvent(
            event_id=str(uuid.uuid4())[:10],
            endpoint=path,
            event_type=event_type,
            payload=payload,
            headers=dict(request.headers),
        )

        self._events.append(event)
        self._stats["received"] += 1
        await self._queue.put(event)
        self._persist(event)

        if self.redis:
            asyncio.create_task(
                self.redis.lpush(f"{REDIS_PREFIX}events", json.dumps(event.to_dict()))
            )
            asyncio.create_task(self.redis.ltrim(f"{REDIS_PREFIX}events", 0, 9999))

        return web.Response(
            status=200,
            text=json.dumps({"event_id": event.event_id}),
            content_type="application/json",
        )

    def _persist(self, event: WebhookEvent):
        with open(EVENTS_FILE, "a") as f:
            f.write(json.dumps(event.to_dict()) + "\n")

    async def _dispatch_loop(self):
        while True:
            event = await self._queue.get()
            handlers = self._handlers.get(event.endpoint, [])
            for handler in handlers:
                try:
                    if asyncio.iscoroutinefunction(handler):
                        await handler(event)
                    else:
                        handler(event)
                    self._stats["processed"] += 1
                    event.processed = True
                except Exception as e:
                    self._stats["errors"] += 1
                    event.error = str(e)[:200]
                    log.error(f"Handler error for {event.event_id}: {e}")

    async def start(self):
        self._app = web.Application()
        for path in self._endpoints:
            self._app.router.add_post(path, self._handle_request)

        asyncio.create_task(self._dispatch_loop())

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self.port)
        await site.start()
        log.info(f"Webhook receiver listening on :{self.port}")

    async def stop(self):
        if self._runner:
            await self._runner.cleanup()

    def stats(self) -> dict:
        return {
            **self._stats,
            "endpoints": len(self._endpoints),
            "queue_depth": self._queue.qsize(),
            "total_events": len(self._events),
        }


async def main():
    import sys

    receiver = WebhookReceiver(port=DEFAULT_PORT)
    await receiver.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        receiver.register_endpoint(
            WebhookEndpoint(
                name="github",
                path="/webhook/github",
                secret="test-secret",
                allowed_events=["push", "pull_request"],
            )
        )

        events_received = []

        async def on_github(event: WebhookEvent):
            events_received.append(event.event_type)
            log.info(f"GitHub event: {event.event_type} [{event.event_id}]")

        receiver.on_event("/webhook/github", on_github)
        await receiver.start()

        # Simulate a push via aiohttp client
        import aiohttp

        payload = json.dumps({"event": "push", "ref": "refs/heads/main"}).encode()
        sig = "sha256=" + hmac.new(b"test-secret", payload, hashlib.sha256).hexdigest()

        async with aiohttp.ClientSession() as sess:
            async with sess.post(
                f"http://localhost:{DEFAULT_PORT}/webhook/github",
                data=payload,
                headers={"X-Signature-256": sig, "Content-Type": "application/json"},
            ) as r:
                print(f"POST response: {r.status} {await r.text()}")

        await asyncio.sleep(0.1)
        print(f"Events received: {events_received}")
        print(f"Stats: {json.dumps(receiver.stats(), indent=2)}")
        await receiver.stop()

    elif cmd == "serve":
        receiver.register_endpoint(WebhookEndpoint(name="default", path="/webhook"))
        receiver.on_event(
            "/webhook", lambda e: print(f"Event: {e.event_type} {e.payload}")
        )
        await receiver.start()
        print(f"Listening on :{DEFAULT_PORT} — Ctrl+C to stop")
        try:
            await asyncio.sleep(3600)
        except KeyboardInterrupt:
            pass
        await receiver.stop()


if __name__ == "__main__":
    asyncio.run(main())
