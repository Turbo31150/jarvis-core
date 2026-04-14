#!/usr/bin/env python3
"""
jarvis_conversation_summarizer — Rolling conversation summarization
Condenses long conversation histories to stay within context limits
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.conversation_summarizer")

LM_URL = "http://127.0.0.1:1234"
SUMMARY_MODEL = "qwen/qwen3.5-9b"
REDIS_PREFIX = "jarvis:summ:"

CHARS_PER_TOKEN = 4
DEFAULT_MAX_TOKENS = 4096
DEFAULT_SUMMARY_EVERY = 10  # summarize after this many messages
DEFAULT_KEEP_RECENT = 4  # always keep last N messages verbatim


@dataclass
class SummaryRecord:
    conv_id: str
    summary: str
    message_count: int
    token_estimate: int
    model: str
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "conv_id": self.conv_id,
            "summary": self.summary,
            "message_count": self.message_count,
            "token_estimate": self.token_estimate,
            "model": self.model,
            "ts": self.ts,
        }


@dataclass
class ManagedConversation:
    conv_id: str
    system: str = ""
    messages: list[dict] = field(default_factory=list)
    summaries: list[SummaryRecord] = field(default_factory=list)
    total_messages: int = 0  # including summarized ones
    created_at: float = field(default_factory=time.time)

    @property
    def effective_context(self) -> list[dict]:
        """Returns system + latest summary (as context) + recent messages."""
        parts = []
        if self.system:
            parts.append({"role": "system", "content": self.system})
        if self.summaries:
            last_summary = self.summaries[-1]
            parts.append(
                {
                    "role": "system",
                    "content": f"[Conversation summary so far]\n{last_summary.summary}",
                }
            )
        parts.extend(self.messages)
        return parts

    def to_dict(self) -> dict:
        return {
            "conv_id": self.conv_id,
            "message_count": len(self.messages),
            "total_messages": self.total_messages,
            "summaries": len(self.summaries),
            "created_at": self.created_at,
        }


class ConversationSummarizer:
    def __init__(
        self,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        summary_every: int = DEFAULT_SUMMARY_EVERY,
        keep_recent: int = DEFAULT_KEEP_RECENT,
    ):
        self.redis: aioredis.Redis | None = None
        self.max_tokens = max_tokens
        self.summary_every = summary_every
        self.keep_recent = keep_recent
        self._conversations: dict[str, ManagedConversation] = {}

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def _token_estimate(self, messages: list[dict]) -> int:
        total = 0
        for m in messages:
            content = m.get("content", "")
            total += len(str(content)) // CHARS_PER_TOKEN + 4
        return total

    def get_or_create(self, conv_id: str, system: str = "") -> ManagedConversation:
        if conv_id not in self._conversations:
            self._conversations[conv_id] = ManagedConversation(
                conv_id=conv_id, system=system
            )
        return self._conversations[conv_id]

    async def add_message(
        self,
        conv_id: str,
        role: str,
        content: Any,
        system: str = "",
        auto_summarize: bool = True,
    ) -> ManagedConversation:
        conv = self.get_or_create(conv_id, system)
        conv.messages.append({"role": role, "content": content})
        conv.total_messages += 1

        if auto_summarize:
            tokens = self._token_estimate(conv.effective_context)
            should_summarize = (
                tokens > self.max_tokens * 0.8
                or len(conv.messages) >= self.summary_every
            )
            if should_summarize:
                await self._summarize(conv)

        return conv

    async def _summarize(self, conv: ManagedConversation):
        """Summarize all but the last `keep_recent` messages."""
        if len(conv.messages) <= self.keep_recent:
            return

        to_summarize = conv.messages[: -self.keep_recent]
        recent = conv.messages[-self.keep_recent :]

        # Build text for summarization
        text_parts = []
        if conv.summaries:
            text_parts.append(f"Previous summary:\n{conv.summaries[-1].summary}\n")
        for msg in to_summarize:
            role = msg.get("role", "unknown")
            content = str(msg.get("content", ""))[:500]
            text_parts.append(f"{role.upper()}: {content}")

        full_text = "\n".join(text_parts)

        summary_text = await self._call_llm_summary(full_text)

        record = SummaryRecord(
            conv_id=conv.conv_id,
            summary=summary_text,
            message_count=len(to_summarize),
            token_estimate=self._token_estimate(to_summarize),
            model=SUMMARY_MODEL,
        )
        conv.summaries.append(record)
        conv.messages = recent

        if self.redis:
            await self.redis.setex(
                f"{REDIS_PREFIX}{conv.conv_id}",
                86400,
                json.dumps(
                    {
                        **conv.to_dict(),
                        "last_summary": record.to_dict(),
                        "recent_messages": recent,
                    }
                ),
            )

        log.info(
            f"Summarized [{conv.conv_id}]: {len(to_summarize)} messages → "
            f"{len(summary_text)} chars"
        )

    async def _call_llm_summary(self, text: str) -> str:
        prompt = (
            "Summarize the following conversation history concisely. "
            "Preserve key facts, decisions, and context. "
            "Write in third person. Return only the summary.\n\n"
            f"{text[:3000]}"
        )
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            ) as sess:
                async with sess.post(
                    f"{LM_URL}/v1/chat/completions",
                    json={
                        "model": SUMMARY_MODEL,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 400,
                        "temperature": 0.2,
                    },
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            log.debug(f"LLM summary failed: {e}")

        # Fallback: extractive summary
        lines = text.split("\n")
        important = [
            l
            for l in lines
            if any(
                kw in l.lower()
                for kw in ["user:", "assistant:", "error", "result", "done"]
            )
        ]
        return " | ".join(important[:10])

    def get_context(self, conv_id: str) -> list[dict]:
        conv = self._conversations.get(conv_id)
        return conv.effective_context if conv else []

    def force_summarize(self, conv_id: str) -> "asyncio.Task":
        conv = self._conversations.get(conv_id)
        if conv:
            return asyncio.create_task(self._summarize(conv))
        raise ValueError(f"Conversation '{conv_id}' not found")

    def stats(self) -> dict:
        convs = list(self._conversations.values())
        return {
            "conversations": len(convs),
            "total_messages": sum(c.total_messages for c in convs),
            "total_summaries": sum(len(c.summaries) for c in convs),
            "avg_messages": round(
                sum(len(c.messages) for c in convs) / max(len(convs), 1), 1
            ),
        }


async def main():
    import sys

    summ = ConversationSummarizer(max_tokens=500, summary_every=6, keep_recent=2)
    await summ.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        conv_id = "demo_conv"
        exchanges = [
            ("user", "What is Redis?"),
            (
                "assistant",
                "Redis is an in-memory data store used as a cache and message broker.",
            ),
            ("user", "What data structures does it support?"),
            (
                "assistant",
                "Redis supports strings, hashes, lists, sets, sorted sets, streams, and more.",
            ),
            ("user", "How do I install it?"),
            (
                "assistant",
                "Install via apt: sudo apt install redis-server, then start with systemctl start redis.",
            ),
            ("user", "How do I connect from Python?"),
            (
                "assistant",
                "Use the redis-py library: pip install redis, then import redis; r = redis.Redis().",
            ),
            ("user", "What about async?"),
            (
                "assistant",
                "Use redis.asyncio: import redis.asyncio as aioredis; r = aioredis.Redis().",
            ),
        ]

        for role, content in exchanges:
            conv = await summ.add_message(
                conv_id, role, content, system="You are JARVIS."
            )
            ctx_tokens = summ._token_estimate(conv.effective_context)
            print(
                f"  [{role}] msgs={len(conv.messages)} summaries={len(conv.summaries)} ctx_tokens≈{ctx_tokens}"
            )

        print(f"\nFinal context ({len(summ.get_context(conv_id))} parts):")
        for msg in summ.get_context(conv_id):
            role = msg["role"]
            content = str(msg["content"])[:80]
            print(f"  [{role}] {content}")

        print(f"\nStats: {json.dumps(summ.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(summ.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
