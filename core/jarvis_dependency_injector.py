#!/usr/bin/env python3
"""
jarvis_dependency_injector — Lightweight IoC container with async support
Constructor injection, singleton/transient scopes, factory registration
"""

import asyncio
import inspect
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Type, TypeVar

log = logging.getLogger("jarvis.dependency_injector")

T = TypeVar("T")


class Scope(str, Enum):
    SINGLETON = "singleton"
    TRANSIENT = "transient"
    REQUEST = "request"  # new instance per resolve chain


@dataclass
class Registration:
    key: str
    factory: Callable
    scope: Scope
    tags: list[str] = field(default_factory=list)
    registered_at: float = field(default_factory=time.time)


@dataclass
class ResolveTrace:
    key: str
    scope: Scope
    duration_ms: float
    from_cache: bool


class Container:
    def __init__(self, name: str = "default"):
        self.name = name
        self._registrations: dict[str, Registration] = {}
        self._singletons: dict[str, Any] = {}
        self._resolve_traces: list[ResolveTrace] = []
        self._stats = {"resolves": 0, "singleton_hits": 0, "creations": 0}

    def register(
        self,
        key: str,
        factory: Callable,
        scope: Scope = Scope.TRANSIENT,
        tags: list[str] | None = None,
    ) -> "Container":
        self._registrations[key] = Registration(
            key=key, factory=factory, scope=scope, tags=tags or []
        )
        log.debug(f"Registered [{key}] scope={scope.value}")
        return self  # fluent

    def register_instance(self, key: str, instance: Any) -> "Container":
        """Register a pre-built singleton."""
        self._registrations[key] = Registration(
            key=key, factory=lambda: instance, scope=Scope.SINGLETON
        )
        self._singletons[key] = instance
        return self

    def register_class(
        self,
        cls: Type[T],
        scope: Scope = Scope.TRANSIENT,
        key: str | None = None,
    ) -> "Container":
        """Auto-register class with constructor injection."""
        k = key or cls.__name__
        self._registrations[k] = Registration(key=k, factory=cls, scope=scope)
        return self

    async def resolve(self, key: str, _chain: set | None = None) -> Any:
        chain = _chain or set()
        if key in chain:
            raise RuntimeError(f"Circular dependency: {key} already in chain {chain}")
        chain = chain | {key}

        reg = self._registrations.get(key)
        if not reg:
            raise KeyError(f"No registration for '{key}'")

        self._stats["resolves"] += 1
        t0 = time.time()

        # Singleton: return cached
        if reg.scope == Scope.SINGLETON and key in self._singletons:
            self._stats["singleton_hits"] += 1
            dur = (time.time() - t0) * 1000
            self._resolve_traces.append(ResolveTrace(key, reg.scope, dur, True))
            return self._singletons[key]

        # Inspect factory for dependencies
        instance = await self._build(reg.factory, chain)

        if reg.scope == Scope.SINGLETON:
            self._singletons[key] = instance

        self._stats["creations"] += 1
        dur = (time.time() - t0) * 1000
        self._resolve_traces.append(ResolveTrace(key, reg.scope, dur, False))
        return instance

    async def _build(self, factory: Callable, chain: set) -> Any:
        sig = inspect.signature(factory)
        kwargs: dict[str, Any] = {}

        for param_name, param in sig.parameters.items():
            if param_name in ("self", "args", "kwargs"):
                continue
            annotation = param.annotation
            # Try to resolve by annotation class name first, then param name
            dep_key = None
            if annotation != inspect.Parameter.empty and isinstance(annotation, type):
                dep_key = annotation.__name__
                if dep_key not in self._registrations:
                    dep_key = None
            if dep_key is None and param_name in self._registrations:
                dep_key = param_name

            if dep_key:
                kwargs[param_name] = await self.resolve(dep_key, chain)
            elif param.default != inspect.Parameter.empty:
                pass  # use default
            # else: leave unset (may fail at instantiation — that's intentional)

        if asyncio.iscoroutinefunction(factory):
            return await factory(**kwargs)
        elif inspect.iscoroutine(factory):
            return await factory
        else:
            return factory(**kwargs)

    def resolve_sync(self, key: str) -> Any:
        return asyncio.get_event_loop().run_until_complete(self.resolve(key))

    def has(self, key: str) -> bool:
        return key in self._registrations

    def keys_by_tag(self, tag: str) -> list[str]:
        return [k for k, r in self._registrations.items() if tag in r.tags]

    def stats(self) -> dict:
        return {
            **self._stats,
            "registrations": len(self._registrations),
            "singleton_cache": len(self._singletons),
            "hit_rate": round(
                self._stats["singleton_hits"] / max(self._stats["resolves"], 1) * 100, 1
            ),
        }

    def list_registrations(self) -> list[dict]:
        return [
            {"key": r.key, "scope": r.scope.value, "tags": r.tags}
            for r in self._registrations.values()
        ]


# Global container
_container = Container("global")


def get_container() -> Container:
    return _container


async def main():
    import sys

    container = Container("demo")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":

        class Config:
            def __init__(self):
                self.model = "qwen3.5-9b"
                self.host = "127.0.0.1:1234"

        class Logger:
            def __init__(self):
                self.level = "debug"

        class LLMClient:
            def __init__(self, Config=None, Logger=None):
                self.config = Config
                self.logger = Logger

        container.register_class(Config, scope=Scope.SINGLETON)
        container.register_class(Logger, scope=Scope.SINGLETON)
        container.register_class(LLMClient, scope=Scope.TRANSIENT)

        client1 = await container.resolve("LLMClient")
        client2 = await container.resolve("LLMClient")
        config1 = await container.resolve("Config")
        config2 = await container.resolve("Config")

        print(f"LLMClient transient: same? {client1 is client2}")
        print(f"Config singleton: same? {config1 is config2}")
        print(f"Config injected: {client1.config is config1}")
        print(f"\nStats: {json.dumps(container.stats(), indent=2)}")
        print("\nRegistrations:")
        for reg in container.list_registrations():
            print(f"  {reg['key']:<20} {reg['scope']}")

    elif cmd == "stats":
        print(json.dumps(get_container().stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
