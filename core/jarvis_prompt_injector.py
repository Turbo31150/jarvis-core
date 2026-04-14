#!/usr/bin/env python3
"""
jarvis_prompt_injector — Dynamic context injection into LLM prompts
Injects RAG results, memory snippets, tool outputs, and system facts at runtime
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.prompt_injector")

LM_URL = "http://127.0.0.1:1234"
REDIS_PREFIX = "jarvis:inject:"


@dataclass
class InjectionSlot:
    name: str
    position: str  # system | pre_user | post_user | tool_result
    content: str
    priority: int = 5  # 1=highest, 10=lowest
    max_tokens: int = 500
    ttl_s: float = 0.0  # 0 = no expiry
    injected_at: float = field(default_factory=time.time)

    @property
    def expired(self) -> bool:
        if not self.ttl_s:
            return False
        return time.time() - self.injected_at > self.ttl_s

    def truncate(self) -> str:
        chars = self.max_tokens * 4
        if len(self.content) > chars:
            return self.content[:chars] + "…"
        return self.content


@dataclass
class InjectionResult:
    messages: list[dict]
    injected: list[str]  # slot names that were injected
    skipped: list[str]  # slots skipped (expired or filtered)
    total_added_tokens: int


class PromptInjector:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._slots: dict[str, InjectionSlot] = {}
        self._register_builtins()

    def _register_builtins(self):
        """Register always-available system slots."""
        self.register(
            InjectionSlot(
                name="datetime",
                position="system",
                content="",  # filled dynamically
                priority=1,
            )
        )
        self.register(
            InjectionSlot(
                name="cluster_status",
                position="system",
                content="",
                priority=2,
                ttl_s=300,
            )
        )

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def register(self, slot: InjectionSlot):
        self._slots[slot.name] = slot

    def unregister(self, name: str):
        self._slots.pop(name, None)

    def set_content(self, name: str, content: str, ttl_s: float = 0.0):
        if name in self._slots:
            self._slots[name].content = content
            self._slots[name].injected_at = time.time()
            if ttl_s:
                self._slots[name].ttl_s = ttl_s
        else:
            self.register(
                InjectionSlot(
                    name=name,
                    position="system",
                    content=content,
                    ttl_s=ttl_s,
                )
            )

    async def _refresh_dynamic_slots(self):
        """Update time-sensitive slots."""
        self._slots[
            "datetime"
        ].content = f"Current date/time: {time.strftime('%Y-%m-%d %H:%M:%S')}"
        self._slots["datetime"].injected_at = time.time()

        # Cluster status from Redis if available
        if self.redis:
            try:
                status = await self.redis.get("jarvis:cluster:status")
                if status:
                    self._slots["cluster_status"].content = f"Cluster: {status}"
                    self._slots["cluster_status"].injected_at = time.time()
            except Exception:
                pass

    def inject(
        self,
        messages: list[dict],
        slots: list[str] | None = None,
        max_total_tokens: int = 1000,
    ) -> InjectionResult:
        """Inject registered slots into messages list."""
        injected = []
        skipped = []
        added_tokens = 0

        # Filter slots
        active_slots = [
            s
            for s in sorted(self._slots.values(), key=lambda x: x.priority)
            if (slots is None or s.name in slots)
            and not s.expired
            and s.content.strip()
        ]

        # Categorize by position
        by_position: dict[str, list[InjectionSlot]] = {}
        for slot in active_slots:
            by_position.setdefault(slot.position, []).append(slot)

        result_messages = list(messages)

        # Inject into system message
        sys_additions = by_position.get("system", [])
        if sys_additions:
            sys_content_parts = []
            for slot in sys_additions:
                tokens_est = len(slot.content) // 4
                if added_tokens + tokens_est > max_total_tokens:
                    skipped.append(slot.name)
                    continue
                sys_content_parts.append(slot.truncate())
                added_tokens += tokens_est
                injected.append(slot.name)

            if sys_content_parts:
                injection_block = "\n".join(sys_content_parts)
                # Find or create system message
                sys_idx = next(
                    (
                        i
                        for i, m in enumerate(result_messages)
                        if m.get("role") == "system"
                    ),
                    None,
                )
                if sys_idx is not None:
                    result_messages[sys_idx] = {
                        "role": "system",
                        "content": result_messages[sys_idx]["content"]
                        + "\n\n"
                        + injection_block,
                    }
                else:
                    result_messages.insert(
                        0, {"role": "system", "content": injection_block}
                    )

        # Inject pre_user (before first user message)
        pre_slots = by_position.get("pre_user", [])
        if pre_slots:
            first_user_idx = next(
                (i for i, m in enumerate(result_messages) if m.get("role") == "user"),
                None,
            )
            for slot in pre_slots:
                tokens_est = len(slot.content) // 4
                if added_tokens + tokens_est > max_total_tokens:
                    skipped.append(slot.name)
                    continue
                insert_at = (
                    first_user_idx
                    if first_user_idx is not None
                    else len(result_messages)
                )
                result_messages.insert(
                    insert_at,
                    {
                        "role": "system",
                        "content": slot.truncate(),
                    },
                )
                added_tokens += tokens_est
                injected.append(slot.name)
                if first_user_idx is not None:
                    first_user_idx += 1

        # Inject post_user (after last user message)
        post_slots = by_position.get("post_user", [])
        if post_slots:
            last_user_idx = next(
                (
                    i
                    for i in range(len(result_messages) - 1, -1, -1)
                    if result_messages[i].get("role") == "user"
                ),
                None,
            )
            if last_user_idx is not None:
                for slot in post_slots:
                    tokens_est = len(slot.content) // 4
                    if added_tokens + tokens_est > max_total_tokens:
                        skipped.append(slot.name)
                        continue
                    # Append to user message content
                    result_messages[last_user_idx] = {
                        "role": "user",
                        "content": result_messages[last_user_idx]["content"]
                        + f"\n\n[Context: {slot.truncate()}]",
                    }
                    added_tokens += tokens_est
                    injected.append(slot.name)

        return InjectionResult(
            messages=result_messages,
            injected=injected,
            skipped=skipped,
            total_added_tokens=added_tokens,
        )

    async def inject_and_infer(
        self,
        messages: list[dict],
        model: str = "qwen/qwen3.5-9b",
        slots: list[str] | None = None,
        max_tokens: int = 1024,
    ) -> str:
        await self._refresh_dynamic_slots()
        result = self.inject(messages, slots=slots)
        log.debug(
            f"Injected: {result.injected} | Skipped: {result.skipped} | +{result.total_added_tokens} tokens"
        )

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60)
            ) as sess:
                async with sess.post(
                    f"{LM_URL}/v1/chat/completions",
                    json={
                        "model": model,
                        "messages": result.messages,
                        "max_tokens": max_tokens,
                        "temperature": 0.7,
                    },
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            log.error(f"Infer error: {e}")
        return ""

    def list_slots(self) -> list[dict]:
        return [
            {
                "name": s.name,
                "position": s.position,
                "priority": s.priority,
                "content_preview": s.content[:60] + "…"
                if len(s.content) > 60
                else s.content,
                "expired": s.expired,
                "ttl_s": s.ttl_s,
            }
            for s in sorted(self._slots.values(), key=lambda x: x.priority)
        ]


async def main():
    import sys

    injector = PromptInjector()
    await injector.connect_redis()
    await injector._refresh_dynamic_slots()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        injector.set_content(
            "rag_context", "Redis key 'jarvis:model:active' = 'qwen3.5-27b'", ttl_s=60
        )
        injector.register(
            InjectionSlot(
                name="user_facts",
                position="pre_user",
                content="User prefers concise answers. Cluster has 5 GPUs.",
                priority=3,
            )
        )

        messages = [
            {"role": "system", "content": "You are JARVIS."},
            {"role": "user", "content": "What model is currently active?"},
        ]
        result = injector.inject(messages)
        print(f"Injected: {result.injected}")
        print(f"Added tokens: {result.total_added_tokens}")
        print(json.dumps(result.messages, indent=2))

    elif cmd == "list":
        for s in injector.list_slots():
            expired = " [EXPIRED]" if s["expired"] else ""
            print(
                f"  [{s['priority']}] {s['name']:<20} {s['position']:<12} {s['content_preview']}{expired}"
            )

    elif cmd == "set" and len(sys.argv) > 3:
        injector.set_content(sys.argv[2], sys.argv[3])
        print(f"Set slot '{sys.argv[2]}'")


if __name__ == "__main__":
    asyncio.run(main())
