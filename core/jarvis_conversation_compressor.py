#!/usr/bin/env python3
"""
jarvis_conversation_compressor — LLM-based conversation summarization and compression
Compresses long histories into structured summaries preserving key facts and context
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from enum import Enum

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.conversation_compressor")

REDIS_PREFIX = "jarvis:compress:"


class CompressionMode(str, Enum):
    SUMMARY = "summary"  # replace with prose summary
    BULLETS = "bullets"  # compress to bullet points
    KEY_FACTS = "key_facts"  # extract key facts only
    HYBRID = "hybrid"  # summary + key facts


@dataclass
class CompressionConfig:
    mode: CompressionMode = CompressionMode.SUMMARY
    max_summary_tokens: int = 300
    min_messages_to_compress: int = 6  # don't compress short conversations
    preserve_last_n: int = 2  # always keep last N turns intact
    compress_url: str = "http://192.168.1.85:1234"
    compress_model: str = "qwen3.5-9b"
    inject_as_system: bool = True  # inject summary as system message


@dataclass
class CompressionResult:
    original_count: int
    compressed_messages: list[dict]
    summary: str
    original_chars: int
    compressed_chars: int
    duration_ms: float
    model_used: str

    @property
    def compression_ratio(self) -> float:
        return 1.0 - (self.compressed_chars / max(self.original_chars, 1))

    def to_dict(self) -> dict:
        return {
            "original_count": self.original_count,
            "compressed_count": len(self.compressed_messages),
            "original_chars": self.original_chars,
            "compressed_chars": self.compressed_chars,
            "compression_ratio": round(self.compression_ratio, 3),
            "duration_ms": round(self.duration_ms, 1),
            "model": self.model_used,
        }


SUMMARY_PROMPTS = {
    CompressionMode.SUMMARY: (
        "Summarize the following conversation in {max_tokens} tokens or less. "
        "Preserve all key decisions, facts, code snippets, and context needed to continue. "
        "Be concise but complete."
    ),
    CompressionMode.BULLETS: (
        "Extract the most important points from this conversation as bullet points. "
        "Limit to {max_tokens} tokens. Include: decisions made, facts established, open questions."
    ),
    CompressionMode.KEY_FACTS: (
        "Extract only the key facts, decisions, and context from this conversation. "
        "Format as a concise list. Max {max_tokens} tokens."
    ),
    CompressionMode.HYBRID: (
        "Summarize this conversation in two sections:\n"
        "1. SUMMARY (2-3 sentences)\n"
        "2. KEY FACTS (bullet points)\n"
        "Total max {max_tokens} tokens."
    ),
}


class ConversationCompressor:
    def __init__(self, config: CompressionConfig | None = None):
        self.redis: aioredis.Redis | None = None
        self._config = config or CompressionConfig()
        self._stats: dict[str, int] = {
            "compressions": 0,
            "skipped": 0,
            "errors": 0,
            "chars_saved": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def should_compress(self, messages: list[dict]) -> bool:
        cfg = self._config
        non_sys = [m for m in messages if m.get("role") != "system"]
        return len(non_sys) >= cfg.min_messages_to_compress

    def _split_messages(
        self, messages: list[dict]
    ) -> tuple[list[dict], list[dict], list[dict]]:
        """Split into system, compressible body, tail (preserved)."""
        cfg = self._config
        system = [m for m in messages if m.get("role") == "system"]
        non_sys = [m for m in messages if m.get("role") != "system"]

        # Keep last N turns (user + assistant pairs) intact
        tail: list[dict] = []
        turns = 0
        for msg in reversed(non_sys):
            tail.insert(0, msg)
            if msg.get("role") == "user":
                turns += 1
            if turns >= cfg.preserve_last_n:
                break

        tail_ids = set(id(m) for m in tail)
        body = [m for m in non_sys if id(m) not in tail_ids]
        return system, body, tail

    def _format_for_summary(self, messages: list[dict]) -> str:
        lines = []
        for msg in messages:
            role = msg.get("role", "user").upper()
            content = msg.get("content", "")[:500]
            lines.append(f"{role}: {content}")
        return "\n\n".join(lines)

    async def _call_llm(self, prompt: str, conversation_text: str) -> str:
        cfg = self._config
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": conversation_text},
        ]
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            ) as sess:
                payload = {
                    "model": cfg.compress_model,
                    "messages": messages,
                    "max_tokens": cfg.max_summary_tokens,
                    "temperature": 0.3,
                }
                async with sess.post(
                    f"{cfg.compress_url}/v1/chat/completions", json=payload
                ) as r:
                    if r.status == 200:
                        data = await r.json(content_type=None)
                        return data["choices"][0]["message"]["content"]
                    else:
                        return ""
        except Exception as e:
            log.warning(f"Compression LLM error: {e}")
            return ""

    def _fallback_summary(self, messages: list[dict]) -> str:
        """Simple extractive fallback when LLM is unavailable."""
        lines = []
        for msg in messages[:10]:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            lines.append(f"[{role.upper()}] {content[:100]}")
        return "Conversation summary (extractive):\n" + "\n".join(lines)

    async def compress(self, messages: list[dict]) -> CompressionResult:
        t0 = time.time()
        self._stats["compressions"] += 1
        cfg = self._config

        if not self.should_compress(messages):
            self._stats["skipped"] += 1
            return CompressionResult(
                original_count=len(messages),
                compressed_messages=list(messages),
                summary="",
                original_chars=sum(len(m.get("content", "")) for m in messages),
                compressed_chars=sum(len(m.get("content", "")) for m in messages),
                duration_ms=(time.time() - t0) * 1000,
                model_used="none",
            )

        system, body, tail = self._split_messages(messages)
        original_chars = sum(len(m.get("content", "")) for m in messages)

        if not body:
            return CompressionResult(
                original_count=len(messages),
                compressed_messages=list(messages),
                summary="",
                original_chars=original_chars,
                compressed_chars=original_chars,
                duration_ms=(time.time() - t0) * 1000,
                model_used="none",
            )

        # Build summary
        prompt_template = SUMMARY_PROMPTS[cfg.mode]
        prompt = prompt_template.format(max_tokens=cfg.max_summary_tokens)
        conversation_text = self._format_for_summary(body)

        summary = await self._call_llm(prompt, conversation_text)
        if not summary:
            summary = self._fallback_summary(body)
            model_used = "fallback"
        else:
            model_used = cfg.compress_model

        # Build compressed message list
        compressed_messages = list(system)
        if cfg.inject_as_system:
            compressed_messages.append(
                {
                    "role": "system",
                    "content": f"[CONVERSATION HISTORY SUMMARY]\n{summary}\n[END SUMMARY]",
                }
            )
        else:
            compressed_messages.append({"role": "assistant", "content": summary})

        compressed_messages.extend(tail)

        compressed_chars = sum(len(m.get("content", "")) for m in compressed_messages)
        self._stats["chars_saved"] += max(0, original_chars - compressed_chars)

        result = CompressionResult(
            original_count=len(messages),
            compressed_messages=compressed_messages,
            summary=summary,
            original_chars=original_chars,
            compressed_chars=compressed_chars,
            duration_ms=(time.time() - t0) * 1000,
            model_used=model_used,
        )

        if self.redis:
            asyncio.create_task(
                self.redis.setex(
                    f"{REDIS_PREFIX}last",
                    3600,
                    json.dumps(result.to_dict()),
                )
            )

        return result

    def stats(self) -> dict:
        return {**self._stats}


async def main():
    import sys

    cfg = CompressionConfig(mode=CompressionMode.BULLETS, preserve_last_n=1)
    compressor = ConversationCompressor(cfg)
    await compressor.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        # Build a long conversation
        messages = [
            {"role": "system", "content": "You are JARVIS."},
        ]
        topics = [
            ("user", "How do I set up Redis on Ubuntu?"),
            (
                "assistant",
                "Install with: sudo apt install redis-server. Then enable: sudo systemctl enable redis.",
            ),
            ("user", "What port does Redis use by default?"),
            (
                "assistant",
                "Redis defaults to port 6379. You can change it in /etc/redis/redis.conf.",
            ),
            ("user", "How do I set a password?"),
            (
                "assistant",
                "Set requirepass in redis.conf, then restart: sudo systemctl restart redis.",
            ),
            ("user", "Can I use Redis for distributed locking?"),
            (
                "assistant",
                "Yes, using SET key value NX PX ttl for atomic acquire. Never use EVAL/Lua for this.",
            ),
            ("user", "What's the GPU temperature on M1 right now?"),
            (
                "assistant",
                "M1 GPU temps: GPU0=65°C GPU1=58°C GPU2=63°C. All within normal range.",
            ),
        ]
        for role, content in topics:
            messages.append({"role": role, "content": content})

        print(
            f"Original: {len(messages)} messages, {sum(len(m['content']) for m in messages)} chars"
        )

        result = await compressor.compress(messages)
        print(
            f"Compressed: {len(result.compressed_messages)} messages, {result.compressed_chars} chars"
        )
        print(f"Ratio: {result.compression_ratio:.1%} saved")
        print(f"Duration: {result.duration_ms:.0f}ms  Model: {result.model_used}")
        print(f"\nSummary preview:\n{result.summary[:300]}")
        print(f"\nStats: {json.dumps(compressor.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(compressor.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
