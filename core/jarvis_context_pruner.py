#!/usr/bin/env python3
"""
jarvis_context_pruner — Smart pruning of conversation history to fit context windows
Removes low-importance messages, summarizes old turns, preserves system+recent context
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

import aiohttp

log = logging.getLogger("jarvis.context_pruner")

LM_URL = "http://127.0.0.1:1234"
SUMMARIZE_MODEL = "qwen/qwen3.5-9b"

# Rough token estimator: 4 chars ≈ 1 token
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


@dataclass
class Message:
    role: str  # system | user | assistant | tool
    content: str
    ts: float = field(default_factory=time.time)
    importance: float = 1.0  # 0.0-1.0
    tokens: int = 0

    def __post_init__(self):
        if not self.tokens:
            self.tokens = estimate_tokens(self.content)

    def to_dict(self) -> dict:
        return {"role": self.role, "content": self.content}


@dataclass
class PruneResult:
    original_count: int
    original_tokens: int
    pruned_count: int
    pruned_tokens: int
    strategy: str
    messages: list[dict]

    @property
    def reduction_pct(self) -> float:
        if not self.original_tokens:
            return 0.0
        return round((1 - self.pruned_tokens / self.original_tokens) * 100, 1)


class ContextPruner:
    def __init__(self, max_tokens: int = 4096, reserve_tokens: int = 512):
        self.max_tokens = max_tokens
        self.reserve_tokens = reserve_tokens  # reserved for response
        self.budget = max_tokens - reserve_tokens

    def _score_importance(self, msg: Message, idx: int, total: int) -> float:
        """Score message importance 0-1. Higher = keep."""
        score = 0.0

        # System messages always max importance
        if msg.role == "system":
            return 1.0

        # Recency boost: last 20% of messages get full score
        recency = idx / max(total - 1, 1)
        score += recency * 0.5

        # Content signals
        content = msg.content.lower()
        if any(
            kw in content for kw in ["error", "exception", "fail", "critical", "urgent"]
        ):
            score += 0.3
        if any(
            kw in content for kw in ["code", "def ", "class ", "function", "import"]
        ):
            score += 0.2
        if len(msg.content) > 500:
            score += 0.1  # detailed messages carry more context
        if msg.role == "user":
            score += 0.15  # user messages slightly more important

        return min(1.0, score)

    def prune_simple(
        self, messages: list[dict], target_tokens: int | None = None
    ) -> PruneResult:
        """Remove lowest-importance messages until budget fits."""
        target = target_tokens or self.budget
        msgs = [Message(**m) if isinstance(m, dict) else m for m in messages]

        original_count = len(msgs)
        original_tokens = sum(m.tokens for m in msgs)

        if original_tokens <= target:
            return PruneResult(
                original_count=original_count,
                original_tokens=original_tokens,
                pruned_count=original_count,
                pruned_tokens=original_tokens,
                strategy="no_prune",
                messages=[m.to_dict() for m in msgs],
            )

        # Score all messages
        for i, msg in enumerate(msgs):
            msg.importance = self._score_importance(msg, i, len(msgs))

        # Always keep: system messages, last 3 messages
        protected = set()
        for i, msg in enumerate(msgs):
            if msg.role == "system":
                protected.add(i)
        for i in range(max(0, len(msgs) - 3), len(msgs)):
            protected.add(i)

        # Sort by importance ascending (remove least important first)
        candidates = sorted(
            [(i, m) for i, m in enumerate(msgs) if i not in protected],
            key=lambda x: x[1].importance,
        )

        total_tokens = original_tokens
        removed = set()
        for idx, msg in candidates:
            if total_tokens <= target:
                break
            removed.add(idx)
            total_tokens -= msg.tokens

        kept = [m.to_dict() for i, m in enumerate(msgs) if i not in removed]
        return PruneResult(
            original_count=original_count,
            original_tokens=original_tokens,
            pruned_count=len(kept),
            pruned_tokens=total_tokens,
            strategy="simple_remove",
            messages=kept,
        )

    def prune_truncate(
        self, messages: list[dict], target_tokens: int | None = None
    ) -> PruneResult:
        """Keep system + last N messages that fit in budget."""
        target = target_tokens or self.budget
        msgs = [Message(**m) if isinstance(m, dict) else m for m in messages]
        original_count = len(msgs)
        original_tokens = sum(m.tokens for m in msgs)

        system_msgs = [m for m in msgs if m.role == "system"]
        other_msgs = [m for m in msgs if m.role != "system"]

        system_tokens = sum(m.tokens for m in system_msgs)
        remaining_budget = target - system_tokens

        kept = list(system_msgs)
        kept_tokens = system_tokens

        # Add from most recent
        for msg in reversed(other_msgs):
            if kept_tokens + msg.tokens <= target:
                kept.insert(len(system_msgs), msg)
                kept_tokens += msg.tokens
            else:
                break

        # Sort by original order
        order = {id(m): i for i, m in enumerate(msgs)}
        kept.sort(key=lambda m: order.get(id(m), 0))

        return PruneResult(
            original_count=original_count,
            original_tokens=original_tokens,
            pruned_count=len(kept),
            pruned_tokens=kept_tokens,
            strategy="truncate_old",
            messages=[m.to_dict() for m in kept],
        )

    async def prune_with_summary(
        self,
        messages: list[dict],
        target_tokens: int | None = None,
    ) -> PruneResult:
        """Summarize middle messages, keep system + recent."""
        target = target_tokens or self.budget
        msgs = [Message(**m) if isinstance(m, dict) else m for m in messages]
        original_count = len(msgs)
        original_tokens = sum(m.tokens for m in msgs)

        if original_tokens <= target:
            return PruneResult(
                original_count=original_count,
                original_tokens=original_tokens,
                pruned_count=original_count,
                pruned_tokens=original_tokens,
                strategy="no_prune",
                messages=[m.to_dict() for m in msgs],
            )

        system_msgs = [m for m in msgs if m.role == "system"]
        other_msgs = [m for m in msgs if m.role != "system"]

        # Keep last 4 messages intact
        recent = other_msgs[-4:]
        to_summarize = other_msgs[:-4]

        summary = ""
        if to_summarize:
            context_text = "\n".join(
                f"{m.role}: {m.content[:300]}" for m in to_summarize
            )
            summary = await self._summarize(context_text)

        summary_msg = (
            Message(
                role="system",
                content=f"[Context summary of earlier conversation]: {summary}",
            )
            if summary
            else None
        )

        kept = list(system_msgs)
        if summary_msg:
            kept.append(summary_msg)
        kept.extend(recent)

        kept_tokens = sum(m.tokens for m in kept)
        return PruneResult(
            original_count=original_count,
            original_tokens=original_tokens,
            pruned_count=len(kept),
            pruned_tokens=kept_tokens,
            strategy="summarize_old",
            messages=[m.to_dict() for m in kept],
        )

    async def _summarize(self, text: str) -> str:
        prompt = f"Summarize this conversation history in 2-3 sentences, preserving key facts and decisions:\n\n{text[:2000]}"
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            ) as sess:
                async with sess.post(
                    f"{LM_URL}/v1/chat/completions",
                    json={
                        "model": SUMMARIZE_MODEL,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 200,
                        "temperature": 0.3,
                    },
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            log.warning(f"Summarize failed: {e}")
        return ""

    def auto_prune(
        self, messages: list[dict], target_tokens: int | None = None
    ) -> PruneResult:
        """Choose strategy based on how much needs to be removed."""
        target = target_tokens or self.budget
        total = sum(estimate_tokens(str(m.get("content", ""))) for m in messages)

        if total <= target:
            return PruneResult(
                original_count=len(messages),
                original_tokens=total,
                pruned_count=len(messages),
                pruned_tokens=total,
                strategy="no_prune",
                messages=messages,
            )

        ratio = total / target
        if ratio < 1.3:
            return self.prune_simple(messages, target)
        else:
            return self.prune_truncate(messages, target)


async def main():
    import sys

    pruner = ContextPruner(max_tokens=2048)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        messages = [
            {"role": "system", "content": "You are JARVIS, an AI assistant."},
        ]
        for i in range(20):
            messages.append(
                {"role": "user", "content": f"Message {i}: " + "x" * (50 + i * 30)}
            )
            messages.append(
                {
                    "role": "assistant",
                    "content": f"Response {i}: " + "y" * (80 + i * 20),
                }
            )

        original_tokens = sum(estimate_tokens(str(m["content"])) for m in messages)
        print(f"Original: {len(messages)} messages, ~{original_tokens} tokens")

        for strategy in ["simple", "truncate"]:
            if strategy == "simple":
                result = pruner.prune_simple(messages)
            else:
                result = pruner.prune_truncate(messages)
            print(f"\nStrategy: {result.strategy}")
            print(f"  {result.original_count} → {result.pruned_count} messages")
            print(
                f"  {result.original_tokens} → {result.pruned_tokens} tokens (-{result.reduction_pct}%)"
            )

    elif cmd == "summarize" and len(sys.argv) > 2:
        with open(sys.argv[2]) as f:
            messages = json.load(f)
        result = await pruner.prune_with_summary(messages)
        print(
            f"Strategy: {result.strategy} | {result.original_tokens} → {result.pruned_tokens} tokens"
        )
        print(json.dumps(result.messages, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
