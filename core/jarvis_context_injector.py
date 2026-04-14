#!/usr/bin/env python3
"""
jarvis_context_injector — Dynamic context injection into LLM conversations
Injects relevant context (memory, RAG, tools, user profile) into message streams
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.context_injector")

REDIS_PREFIX = "jarvis:ctx_inject:"


class InjectionPoint(str, Enum):
    SYSTEM_PREPEND = "system_prepend"  # add to start of system message
    SYSTEM_APPEND = "system_append"  # add to end of system message
    BEFORE_LAST_USER = "before_last_user"  # insert before last user message
    AFTER_SYSTEM = "after_system"  # add new message after system


@dataclass
class ContextFragment:
    key: str
    content: str
    injection_point: InjectionPoint
    priority: int = 5  # higher = injected first in its slot
    max_chars: int = 2000
    ttl_s: float = 0.0  # 0 = no expiry
    created_at: float = field(default_factory=time.time)
    tags: list[str] = field(default_factory=list)

    @property
    def expired(self) -> bool:
        return self.ttl_s > 0 and (time.time() - self.created_at) > self.ttl_s

    @property
    def truncated_content(self) -> str:
        if len(self.content) > self.max_chars:
            return self.content[: self.max_chars] + "...[truncated]"
        return self.content

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "injection_point": self.injection_point.value,
            "priority": self.priority,
            "content_len": len(self.content),
            "expired": self.expired,
            "tags": self.tags,
        }


@dataclass
class InjectionResult:
    original_messages: list[dict]
    injected_messages: list[dict]
    fragments_applied: list[str]
    fragments_skipped: list[str]
    chars_added: int
    duration_ms: float

    def to_dict(self) -> dict:
        return {
            "message_count": len(self.injected_messages),
            "fragments_applied": self.fragments_applied,
            "fragments_skipped": self.fragments_skipped,
            "chars_added": self.chars_added,
            "duration_ms": round(self.duration_ms, 1),
        }


class ContextInjector:
    def __init__(self, max_context_chars: int = 50_000):
        self.redis: aioredis.Redis | None = None
        self._fragments: dict[str, ContextFragment] = {}
        self._dynamic_providers: list[Callable] = []
        self.max_context_chars = max_context_chars
        self._stats: dict[str, int] = {
            "injections": 0,
            "fragments_applied": 0,
            "fragments_expired": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def register(self, fragment: ContextFragment):
        self._fragments[fragment.key] = fragment

    def unregister(self, key: str):
        self._fragments.pop(key, None)

    def add_dynamic_provider(self, provider: Callable):
        """Async fn(messages) → list[ContextFragment]"""
        self._dynamic_providers.append(provider)

    def _get_or_create_system(self, messages: list[dict]) -> tuple[list[dict], int]:
        """Returns (messages_copy, system_idx). Creates system message if absent."""
        msgs = [dict(m) for m in messages]
        for i, m in enumerate(msgs):
            if m.get("role") == "system":
                return msgs, i
        # Insert system message at start
        msgs.insert(0, {"role": "system", "content": ""})
        return msgs, 0

    async def inject(
        self,
        messages: list[dict],
        tags_filter: list[str] | None = None,
        skip_keys: list[str] | None = None,
    ) -> InjectionResult:
        t0 = time.time()
        self._stats["injections"] += 1
        skip = set(skip_keys or [])

        # Collect static fragments
        fragments = list(self._fragments.values())

        # Collect dynamic fragments
        for provider in self._dynamic_providers:
            try:
                if asyncio.iscoroutinefunction(provider):
                    extra = await provider(messages)
                else:
                    extra = provider(messages)
                fragments.extend(extra or [])
            except Exception as e:
                log.warning(f"Dynamic provider error: {e}")

        # Filter
        active = []
        skipped = []
        for frag in fragments:
            if frag.key in skip:
                skipped.append(frag.key)
                continue
            if frag.expired:
                self._stats["fragments_expired"] += 1
                skipped.append(frag.key)
                continue
            if tags_filter and not any(t in frag.tags for t in tags_filter):
                skipped.append(frag.key)
                continue
            active.append(frag)

        # Sort by injection point then priority desc
        active.sort(key=lambda f: (f.injection_point.value, -f.priority))

        msgs, sys_idx = self._get_or_create_system(messages)
        applied = []
        chars_added = 0

        for frag in active:
            content = frag.truncated_content
            ip = frag.injection_point

            if ip == InjectionPoint.SYSTEM_PREPEND:
                current = msgs[sys_idx].get("content", "")
                msgs[sys_idx]["content"] = (
                    content + "\n\n" + current if current else content
                )

            elif ip == InjectionPoint.SYSTEM_APPEND:
                current = msgs[sys_idx].get("content", "")
                msgs[sys_idx]["content"] = (
                    (current + "\n\n" + content) if current else content
                )

            elif ip == InjectionPoint.AFTER_SYSTEM:
                msgs.insert(sys_idx + 1, {"role": "system", "content": content})
                sys_idx += 1  # adjust index

            elif ip == InjectionPoint.BEFORE_LAST_USER:
                # Find last user message
                last_user_idx = next(
                    (
                        i
                        for i in range(len(msgs) - 1, -1, -1)
                        if msgs[i].get("role") == "user"
                    ),
                    None,
                )
                if last_user_idx is not None:
                    msgs.insert(last_user_idx, {"role": "system", "content": content})

            applied.append(frag.key)
            chars_added += len(content)
            self._stats["fragments_applied"] += 1

        return InjectionResult(
            original_messages=messages,
            injected_messages=msgs,
            fragments_applied=applied,
            fragments_skipped=skipped,
            chars_added=chars_added,
            duration_ms=(time.time() - t0) * 1000,
        )

    def list_fragments(self) -> list[dict]:
        return [f.to_dict() for f in self._fragments.values()]

    def stats(self) -> dict:
        return {
            **self._stats,
            "registered_fragments": len(self._fragments),
            "dynamic_providers": len(self._dynamic_providers),
        }


def build_jarvis_injector() -> ContextInjector:
    injector = ContextInjector()
    injector.register(
        ContextFragment(
            key="jarvis_identity",
            content="You are JARVIS, a multi-agent AI orchestration system on a GPU cluster. Be concise and technical.",
            injection_point=InjectionPoint.SYSTEM_PREPEND,
            priority=10,
            tags=["identity"],
        )
    )
    injector.register(
        ContextFragment(
            key="cluster_info",
            content="Cluster: M1 (192.168.1.85, 3 GPUs), M2 (192.168.1.26, 1 GPU), OL1 (local Ollama). Redis on localhost:6379.",
            injection_point=InjectionPoint.SYSTEM_APPEND,
            priority=8,
            tags=["cluster", "infra"],
        )
    )
    return injector


async def main():
    import sys

    injector = build_jarvis_injector()
    await injector.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        messages = [
            {"role": "user", "content": "What models are available on M1?"},
        ]

        # Add a dynamic provider
        async def memory_provider(msgs):
            return [
                ContextFragment(
                    key="recent_memory",
                    content="Recent: User asked about cluster health 5 minutes ago.",
                    injection_point=InjectionPoint.BEFORE_LAST_USER,
                    priority=6,
                    tags=["memory"],
                    ttl_s=300,
                )
            ]

        injector.add_dynamic_provider(memory_provider)

        result = await injector.inject(messages)
        print(f"Applied: {result.fragments_applied}")
        print(f"Chars added: {result.chars_added}")
        print("\nInjected messages:")
        for msg in result.injected_messages:
            role = msg["role"]
            content = msg["content"][:100].replace("\n", " ")
            print(f"  [{role}] {content}...")

        print(f"\nStats: {json.dumps(injector.stats(), indent=2)}")

    elif cmd == "list":
        for f in injector.list_fragments():
            print(f"  {f['key']:<30} {f['injection_point']:<20} p={f['priority']}")

    elif cmd == "stats":
        print(json.dumps(injector.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
