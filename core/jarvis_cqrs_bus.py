#!/usr/bin/env python3
"""
jarvis_cqrs_bus — CQRS command/query bus with handler registry
Commands mutate state, Queries read state — strict separation with middleware
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.cqrs_bus")

REDIS_PREFIX = "jarvis:cqrs:"


class MessageKind(str, Enum):
    COMMAND = "command"
    QUERY = "query"
    EVENT = "event"


@dataclass
class Message:
    name: str
    kind: MessageKind
    payload: dict[str, Any] = field(default_factory=dict)
    message_id: str = ""
    correlation_id: str = ""
    ts: float = field(default_factory=time.time)
    source: str = ""

    def __post_init__(self):
        if not self.message_id:
            import secrets

            self.message_id = secrets.token_hex(8)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind.value,
            "payload": self.payload,
            "message_id": self.message_id,
            "correlation_id": self.correlation_id,
            "ts": self.ts,
            "source": self.source,
        }


@dataclass
class MessageResult:
    success: bool
    message_id: str
    data: Any = None
    error: str = ""
    duration_ms: float = 0.0
    handler: str = ""

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "message_id": self.message_id,
            "data": self.data,
            "error": self.error,
            "duration_ms": round(self.duration_ms, 2),
            "handler": self.handler,
        }


# Middleware signature: (msg, next) -> MessageResult
MiddlewareFn = Callable[[Message, Callable], "MessageResult"]


class HandlerRegistry:
    def __init__(self):
        self._command_handlers: dict[str, Callable] = {}
        self._query_handlers: dict[str, Callable] = {}
        self._event_handlers: dict[str, list[Callable]] = {}

    def command(self, name: str):
        """Decorator to register a command handler."""

        def decorator(fn):
            self._command_handlers[name] = fn
            return fn

        return decorator

    def query(self, name: str):
        """Decorator to register a query handler."""

        def decorator(fn):
            self._query_handlers[name] = fn
            return fn

        return decorator

    def event(self, name: str):
        """Decorator to register an event handler (multiple allowed)."""

        def decorator(fn):
            self._event_handlers.setdefault(name, []).append(fn)
            return fn

        return decorator

    def register_command(self, name: str, fn: Callable):
        self._command_handlers[name] = fn

    def register_query(self, name: str, fn: Callable):
        self._query_handlers[name] = fn

    def register_event(self, name: str, fn: Callable):
        self._event_handlers.setdefault(name, []).append(fn)

    def get_command(self, name: str) -> Callable | None:
        return self._command_handlers.get(name)

    def get_query(self, name: str) -> Callable | None:
        return self._query_handlers.get(name)

    def get_events(self, name: str) -> list[Callable]:
        return self._event_handlers.get(name, [])

    def list_handlers(self) -> dict:
        return {
            "commands": list(self._command_handlers),
            "queries": list(self._query_handlers),
            "events": {k: len(v) for k, v in self._event_handlers.items()},
        }


class CQRSBus:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self.registry = HandlerRegistry()
        self._middlewares: list[MiddlewareFn] = []
        self._history: list[MessageResult] = []
        self._max_history = 1000
        self._stats: dict[str, int] = {
            "commands": 0,
            "queries": 0,
            "events": 0,
            "success": 0,
            "errors": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def use(self, middleware: MiddlewareFn):
        """Add middleware to the pipeline (LIFO execution order)."""
        self._middlewares.append(middleware)

    async def send(self, msg: Message) -> MessageResult:
        """Dispatch a command (single handler, must exist)."""
        self._stats["commands"] += 1
        handler = self.registry.get_command(msg.name)
        if not handler:
            err = MessageResult(
                success=False,
                message_id=msg.message_id,
                error=f"no handler registered for command {msg.name!r}",
            )
            self._record(err)
            return err
        return await self._invoke(msg, handler)

    async def ask(self, msg: Message) -> MessageResult:
        """Dispatch a query (single handler, returns data)."""
        self._stats["queries"] += 1
        handler = self.registry.get_query(msg.name)
        if not handler:
            err = MessageResult(
                success=False,
                message_id=msg.message_id,
                error=f"no handler for query {msg.name!r}",
            )
            self._record(err)
            return err
        return await self._invoke(msg, handler)

    async def publish(self, msg: Message) -> list[MessageResult]:
        """Publish an event (zero or more handlers, fire-and-forget friendly)."""
        self._stats["events"] += 1
        handlers = self.registry.get_events(msg.name)
        if not handlers:
            log.debug(f"Event {msg.name!r} published with no subscribers")
            return []
        tasks = [self._invoke(msg, h) for h in handlers]
        results = await asyncio.gather(*tasks, return_exceptions=False)
        if self.redis:
            asyncio.create_task(self._redis_publish(msg))
        return list(results)

    async def _invoke(self, msg: Message, handler: Callable) -> MessageResult:
        """Run handler through middleware chain."""
        start = time.time()
        handler_name = getattr(handler, "__name__", str(handler))

        # Build middleware chain
        async def base_call(m: Message) -> MessageResult:
            try:
                if asyncio.iscoroutinefunction(handler):
                    data = await handler(m)
                else:
                    data = handler(m)
                dur = (time.time() - start) * 1000
                result = MessageResult(
                    success=True,
                    message_id=m.message_id,
                    data=data,
                    duration_ms=dur,
                    handler=handler_name,
                )
                self._stats["success"] += 1
                return result
            except Exception as e:
                dur = (time.time() - start) * 1000
                log.error(f"Handler {handler_name!r} failed for {m.name!r}: {e}")
                result = MessageResult(
                    success=False,
                    message_id=m.message_id,
                    error=str(e),
                    duration_ms=dur,
                    handler=handler_name,
                )
                self._stats["errors"] += 1
                return result

        # Wrap with middlewares (reverse order so first registered is outermost)
        call = base_call
        for mw in reversed(self._middlewares):
            outer_mw = mw
            inner_call = call

            async def wrapped(
                m: Message, _mw=outer_mw, _next=inner_call
            ) -> MessageResult:
                return await _mw(m, _next)

            call = wrapped

        result = await call(msg)
        self._record(result)
        return result

    async def _redis_publish(self, msg: Message):
        if not self.redis:
            return
        try:
            await self.redis.xadd(
                f"{REDIS_PREFIX}events",
                {"data": json.dumps(msg.to_dict())},
                maxlen=10000,
            )
        except Exception:
            pass

    def _record(self, result: MessageResult):
        self._history.append(result)
        if len(self._history) > self._max_history:
            self._history.pop(0)

    def recent(self, limit: int = 20) -> list[dict]:
        return [r.to_dict() for r in self._history[-limit:]]

    def stats(self) -> dict:
        total = self._stats["commands"] + self._stats["queries"] + self._stats["events"]
        return {
            **self._stats,
            "total": total,
            "error_rate": round(self._stats["errors"] / max(total, 1), 4),
            "handlers": self.registry.list_handlers(),
        }


# --- Logging middleware ---
async def logging_middleware(msg: Message, next_fn: Callable) -> MessageResult:
    log.debug(f"[CQRS] {msg.kind.value} {msg.name!r} id={msg.message_id}")
    result = await next_fn(msg)
    status = "OK" if result.success else f"ERR:{result.error}"
    log.debug(f"[CQRS] {msg.name!r} → {status} {result.duration_ms:.1f}ms")
    return result


def build_jarvis_cqrs_bus() -> CQRSBus:
    bus = CQRSBus()
    bus.use(logging_middleware)

    # Example handlers for JARVIS cluster operations
    @bus.registry.command("cluster.rebalance")
    async def handle_rebalance(msg: Message) -> dict:
        node = msg.payload.get("node", "auto")
        return {"status": "rebalancing", "node": node, "ts": time.time()}

    @bus.registry.command("model.load")
    async def handle_model_load(msg: Message) -> dict:
        model = msg.payload.get("model", "unknown")
        node = msg.payload.get("node", "m1")
        return {"status": "loading", "model": model, "node": node}

    @bus.registry.query("cluster.status")
    async def handle_cluster_status(msg: Message) -> dict:
        return {"nodes": ["m1", "m2", "ol1"], "status": "healthy", "ts": time.time()}

    @bus.registry.query("model.list")
    async def handle_model_list(msg: Message) -> list:
        return ["qwen3.5-9b", "qwen3.5-35b", "deepseek-r1", "gemma3:4b"]

    @bus.registry.event("inference.completed")
    async def on_inference_done(msg: Message):
        log.debug(
            f"Inference done: {msg.payload.get('model')} {msg.payload.get('latency_ms', 0):.0f}ms"
        )

    @bus.registry.event("alert.fired")
    async def on_alert(msg: Message):
        log.warning(f"Alert: {msg.payload.get('message', '?')}")

    return bus


async def main():
    import sys

    bus = build_jarvis_cqrs_bus()
    await bus.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("CQRS bus demo...")

        # Commands
        r1 = await bus.send(
            Message("cluster.rebalance", MessageKind.COMMAND, {"node": "m2"})
        )
        r2 = await bus.send(
            Message(
                "model.load", MessageKind.COMMAND, {"model": "qwen3.5-9b", "node": "m1"}
            )
        )
        r3 = await bus.send(Message("unknown.cmd", MessageKind.COMMAND, {}))

        # Queries
        r4 = await bus.ask(Message("cluster.status", MessageKind.QUERY))
        r5 = await bus.ask(Message("model.list", MessageKind.QUERY))

        # Events
        results = await bus.publish(
            Message(
                "inference.completed",
                MessageKind.EVENT,
                {"model": "qwen3.5", "latency_ms": 320},
            )
        )

        for r in [r1, r2, r3, r4, r5]:
            icon = "✅" if r.success else "❌"
            data_preview = str(r.data)[:50] if r.data else r.error
            print(f"  {icon} {r.handler or 'N/A':<30} → {data_preview}")

        print(f"\nEvent handlers fired: {len(results)}")
        print(f"\nStats: {json.dumps(bus.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(bus.stats(), indent=2))

    elif cmd == "handlers":
        print(json.dumps(bus.registry.list_handlers(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
