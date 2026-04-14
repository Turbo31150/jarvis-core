#!/usr/bin/env python3
"""
jarvis_context_window_manager — Context window budget management
Tracks token usage per conversation, trims overflow, injects system context
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.context_window_manager")

REDIS_PREFIX = "jarvis:ctx:"

# Approximate tokens per model
MODEL_CONTEXT_LIMITS: dict[str, int] = {
    "qwen/qwen3.5-9b": 32768,
    "qwen/qwen3.5-27b-claude": 32768,
    "qwen/qwen3.5-35b-a3b": 32768,
    "deepseek-r1-0528": 65536,
    "glm-4.7-flash-claude": 8192,
    "gemma-4-26b-a4b": 8192,
    "default": 8192,
}

CHARS_PER_TOKEN = 4  # rough estimate


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


def estimate_message_tokens(msg: dict) -> int:
    content = msg.get("content", "")
    if isinstance(content, list):
        return sum(
            estimate_tokens(p.get("text", "")) for p in content if isinstance(p, dict)
        )
    return estimate_tokens(str(content)) + 4  # role overhead


@dataclass
class ContextBudget:
    model: str
    limit: int
    system_reserved: int = 512
    response_reserved: int = 1024

    @property
    def available(self) -> int:
        return self.limit - self.system_reserved - self.response_reserved

    @property
    def utilization(self) -> float:
        return round(self.system_reserved / max(self.limit, 1), 3)


@dataclass
class ManagedContext:
    conv_id: str
    model: str
    messages: list[dict] = field(default_factory=list)
    system: str = ""
    total_tokens: int = 0
    trims: int = 0
    created_at: float = field(default_factory=time.time)
    last_updated: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "conv_id": self.conv_id,
            "model": self.model,
            "message_count": len(self.messages),
            "total_tokens": self.total_tokens,
            "trims": self.trims,
            "created_at": self.created_at,
            "last_updated": self.last_updated,
        }


class ContextWindowManager:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._contexts: dict[str, ManagedContext] = {}

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def _get_limit(self, model: str) -> int:
        for key, limit in MODEL_CONTEXT_LIMITS.items():
            if key.lower() in model.lower():
                return limit
        return MODEL_CONTEXT_LIMITS["default"]

    def create(self, conv_id: str, model: str, system: str = "") -> ManagedContext:
        ctx = ManagedContext(conv_id=conv_id, model=model, system=system)
        if system:
            ctx.total_tokens += estimate_tokens(system)
        self._contexts[conv_id] = ctx
        return ctx

    def get_or_create(
        self, conv_id: str, model: str, system: str = ""
    ) -> ManagedContext:
        return self._contexts.get(conv_id) or self.create(conv_id, model, system)

    def add_message(
        self, conv_id: str, role: str, content: Any, model: str = "default"
    ) -> int:
        """Add a message and return current token count. Auto-trims if needed."""
        ctx = self.get_or_create(conv_id, model)
        msg = {"role": role, "content": content}
        tokens = estimate_message_tokens(msg)
        ctx.messages.append(msg)
        ctx.total_tokens += tokens
        ctx.last_updated = time.time()

        # Auto-trim if over budget
        limit = self._get_limit(ctx.model)
        budget = ContextBudget(model=ctx.model, limit=limit)
        if ctx.total_tokens > budget.available:
            self._trim(ctx, budget)

        return ctx.total_tokens

    def _trim(self, ctx: ManagedContext, budget: ContextBudget):
        """Remove oldest non-system messages until under budget."""
        target = budget.available - budget.response_reserved
        removed = 0

        while ctx.total_tokens > target and len(ctx.messages) > 2:
            # Keep at least last 2 messages (latest exchange)
            # Remove oldest message that isn't a system message
            for i, msg in enumerate(ctx.messages):
                if msg.get("role") != "system":
                    tokens = estimate_message_tokens(msg)
                    ctx.messages.pop(i)
                    ctx.total_tokens -= tokens
                    removed += 1
                    break
            else:
                break  # can't remove anything

        if removed:
            ctx.trims += 1
            log.debug(
                f"Trimmed [{ctx.conv_id}]: removed {removed} messages, "
                f"now {ctx.total_tokens} tokens"
            )

    def get_messages(
        self,
        conv_id: str,
        include_system: bool = True,
    ) -> list[dict]:
        ctx = self._contexts.get(conv_id)
        if not ctx:
            return []
        msgs = list(ctx.messages)
        if include_system and ctx.system:
            msgs = [{"role": "system", "content": ctx.system}] + msgs
        return msgs

    def inject(self, conv_id: str, content: str, position: str = "pre_user"):
        """Inject content before next user message."""
        ctx = self._contexts.get(conv_id)
        if not ctx:
            return
        tag = f"[injected:{position}]"
        ctx.messages.append({"role": "system", "content": f"{tag} {content}"})
        ctx.total_tokens += estimate_tokens(content)

    def clear(self, conv_id: str, keep_system: bool = True):
        ctx = self._contexts.get(conv_id)
        if not ctx:
            return
        sys_tokens = estimate_tokens(ctx.system) if ctx.system else 0
        ctx.messages.clear()
        ctx.total_tokens = sys_tokens
        log.debug(f"Cleared [{conv_id}]")

    def budget_info(self, conv_id: str) -> dict:
        ctx = self._contexts.get(conv_id)
        if not ctx:
            return {}
        limit = self._get_limit(ctx.model)
        budget = ContextBudget(model=ctx.model, limit=limit)
        return {
            "conv_id": conv_id,
            "model": ctx.model,
            "limit": limit,
            "used": ctx.total_tokens,
            "available": budget.available - ctx.total_tokens,
            "pct": round(ctx.total_tokens / limit * 100, 1),
            "messages": len(ctx.messages),
            "trims": ctx.trims,
        }

    async def persist(self, conv_id: str):
        ctx = self._contexts.get(conv_id)
        if not ctx or not self.redis:
            return
        await self.redis.setex(
            f"{REDIS_PREFIX}{conv_id}",
            3600,
            json.dumps({**ctx.to_dict(), "messages": ctx.messages[-20:]}),
        )

    async def restore(self, conv_id: str) -> ManagedContext | None:
        if not self.redis:
            return None
        raw = await self.redis.get(f"{REDIS_PREFIX}{conv_id}")
        if not raw:
            return None
        d = json.loads(raw)
        ctx = ManagedContext(
            conv_id=d["conv_id"],
            model=d["model"],
            messages=d.get("messages", []),
            total_tokens=d.get("total_tokens", 0),
            trims=d.get("trims", 0),
            created_at=d.get("created_at", time.time()),
        )
        self._contexts[conv_id] = ctx
        return ctx

    def stats(self) -> dict:
        ctxs = list(self._contexts.values())
        return {
            "active_conversations": len(ctxs),
            "total_tokens": sum(c.total_tokens for c in ctxs),
            "total_trims": sum(c.trims for c in ctxs),
            "avg_messages": round(
                sum(len(c.messages) for c in ctxs) / max(len(ctxs), 1), 1
            ),
        }


async def main():
    import sys

    mgr = ContextWindowManager()
    await mgr.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        ctx = mgr.create("conv1", "qwen/qwen3.5-9b", system="You are JARVIS.")
        mgr.add_message("conv1", "user", "Hello, who are you?")
        mgr.add_message("conv1", "assistant", "I am JARVIS, your AI assistant.")
        mgr.add_message("conv1", "user", "What can you do?")

        info = mgr.budget_info("conv1")
        print(
            f"Context [{info['conv_id']}]: {info['used']}/{info['limit']} tokens ({info['pct']}%)"
        )
        print(f"Messages: {info['messages']}, Trims: {info['trims']}")

        msgs = mgr.get_messages("conv1")
        for m in msgs:
            print(f"  [{m['role']}]: {str(m['content'])[:60]}")

    elif cmd == "stats":
        print(json.dumps(mgr.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
