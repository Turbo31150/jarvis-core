#!/usr/bin/env python3
"""
jarvis_context_window_trimmer — Smart truncation of LLM message histories
Trims conversation history to fit token budgets while preserving critical messages
"""

import json
import logging
import time
from dataclasses import dataclass
from enum import Enum

log = logging.getLogger("jarvis.context_window_trimmer")


class TrimStrategy(str, Enum):
    FIFO = "fifo"  # drop oldest non-system messages
    IMPORTANCE = "importance"  # drop lowest-importance messages
    SUMMARIZE = "summarize"  # replace middle with summary placeholder
    SLIDING = "sliding"  # keep system + last N turns


class MessageImportance(int, Enum):
    CRITICAL = 0  # never drop (system, pinned)
    HIGH = 1  # drop last
    NORMAL = 2
    LOW = 3  # drop first


@dataclass
class TrimConfig:
    max_tokens: int = 4096
    strategy: TrimStrategy = TrimStrategy.FIFO
    preserve_system: bool = True
    preserve_last_n_turns: int = 2  # always keep last N user/assistant pairs
    summary_placeholder: str = "[...earlier conversation trimmed...]"
    chars_per_token: float = 4.0  # rough estimate


@dataclass
class TrimResult:
    original_messages: list[dict]
    trimmed_messages: list[dict]
    original_tokens: int
    trimmed_tokens: int
    dropped_count: int
    strategy_used: TrimStrategy
    duration_ms: float

    def to_dict(self) -> dict:
        return {
            "original_count": len(self.original_messages),
            "trimmed_count": len(self.trimmed_messages),
            "original_tokens": self.original_tokens,
            "trimmed_tokens": self.trimmed_tokens,
            "dropped_count": self.dropped_count,
            "strategy": self.strategy_used.value,
            "duration_ms": round(self.duration_ms, 1),
        }


def _estimate_tokens(messages: list[dict], chars_per_token: float = 4.0) -> int:
    total = sum(len(m.get("content", "")) for m in messages)
    return max(1, int(total / chars_per_token))


def _msg_tokens(msg: dict, chars_per_token: float = 4.0) -> int:
    return max(1, int(len(msg.get("content", "")) / chars_per_token))


def _importance(msg: dict) -> MessageImportance:
    role = msg.get("role", "")
    meta = msg.get("_importance", None)
    if meta is not None:
        return MessageImportance(meta)
    if role == "system":
        return MessageImportance.CRITICAL
    if msg.get("_pinned"):
        return MessageImportance.CRITICAL
    # Tool results and errors are high importance
    content = msg.get("content", "").lower()
    if any(kw in content for kw in ["error", "exception", "traceback", "critical"]):
        return MessageImportance.HIGH
    if role == "tool":
        return MessageImportance.HIGH
    return MessageImportance.NORMAL


class ContextWindowTrimmer:
    def __init__(self, config: TrimConfig | None = None):
        self._config = config or TrimConfig()
        self._stats: dict[str, int] = {
            "trim_calls": 0,
            "messages_dropped": 0,
            "tokens_saved": 0,
        }

    def _split_preserve(
        self, messages: list[dict]
    ) -> tuple[list[dict], list[dict], list[dict]]:
        """Split into: [system msgs], [protected tail], [trimmable middle]."""
        cfg = self._config
        system = (
            [m for m in messages if m.get("role") == "system"]
            if cfg.preserve_system
            else []
        )

        non_system = [
            m
            for m in messages
            if not (cfg.preserve_system and m.get("role") == "system")
        ]

        # Protect last N turns (user + assistant pairs)
        tail: list[dict] = []
        turns = 0
        for msg in reversed(non_system):
            if msg.get("role") in ("user", "assistant"):
                tail.insert(0, msg)
                if msg.get("role") == "user":
                    turns += 1
                if turns >= cfg.preserve_last_n_turns:
                    break
            else:
                tail.insert(0, msg)

        tail_set = set(id(m) for m in tail)
        middle = [m for m in non_system if id(m) not in tail_set]
        return system, middle, tail

    def trim(self, messages: list[dict]) -> TrimResult:
        t0 = time.time()
        self._stats["trim_calls"] += 1
        cfg = self._config
        original_tokens = _estimate_tokens(messages, cfg.chars_per_token)

        if original_tokens <= cfg.max_tokens:
            return TrimResult(
                original_messages=messages,
                trimmed_messages=list(messages),
                original_tokens=original_tokens,
                trimmed_tokens=original_tokens,
                dropped_count=0,
                strategy_used=cfg.strategy,
                duration_ms=(time.time() - t0) * 1000,
            )

        system, middle, tail = self._split_preserve(messages)

        if cfg.strategy == TrimStrategy.FIFO:
            result_msgs, dropped = self._trim_fifo(system, middle, tail)
        elif cfg.strategy == TrimStrategy.IMPORTANCE:
            result_msgs, dropped = self._trim_importance(system, middle, tail)
        elif cfg.strategy == TrimStrategy.SUMMARIZE:
            result_msgs, dropped = self._trim_summarize(system, middle, tail)
        elif cfg.strategy == TrimStrategy.SLIDING:
            result_msgs, dropped = self._trim_sliding(system, middle, tail)
        else:
            result_msgs, dropped = system + middle + tail, 0

        trimmed_tokens = _estimate_tokens(result_msgs, cfg.chars_per_token)
        self._stats["messages_dropped"] += dropped
        self._stats["tokens_saved"] += max(0, original_tokens - trimmed_tokens)

        return TrimResult(
            original_messages=messages,
            trimmed_messages=result_msgs,
            original_tokens=original_tokens,
            trimmed_tokens=trimmed_tokens,
            dropped_count=dropped,
            strategy_used=cfg.strategy,
            duration_ms=(time.time() - t0) * 1000,
        )

    def _trim_fifo(self, system, middle, tail) -> tuple[list[dict], int]:
        cfg = self._config
        budget = cfg.max_tokens
        budget -= _estimate_tokens(system + tail, cfg.chars_per_token)
        kept = []
        # Keep from end of middle backward
        for msg in reversed(middle):
            cost = _msg_tokens(msg, cfg.chars_per_token)
            if budget >= cost:
                kept.insert(0, msg)
                budget -= cost
        dropped = len(middle) - len(kept)
        return system + kept + tail, dropped

    def _trim_importance(self, system, middle, tail) -> tuple[list[dict], int]:
        cfg = self._config
        budget = cfg.max_tokens - _estimate_tokens(system + tail, cfg.chars_per_token)
        # Sort middle by importance desc (drop low importance first)
        scored = sorted(middle, key=lambda m: (_importance(m).value, 0), reverse=True)
        kept = []
        for msg in scored:
            cost = _msg_tokens(msg, cfg.chars_per_token)
            if budget >= cost:
                kept.append(msg)
                budget -= cost
        # Restore order
        orig_order = {id(m): i for i, m in enumerate(middle)}
        kept.sort(key=lambda m: orig_order.get(id(m), 0))
        dropped = len(middle) - len(kept)
        return system + kept + tail, dropped

    def _trim_summarize(self, system, middle, tail) -> tuple[list[dict], int]:
        if not middle:
            return system + middle + tail, 0
        placeholder = {"role": "system", "content": self._config.summary_placeholder}
        dropped = len(middle)
        return system + [placeholder] + tail, dropped

    def _trim_sliding(self, system, middle, tail) -> tuple[list[dict], int]:
        # Just drop all middle
        dropped = len(middle)
        return system + tail, dropped

    def fits(self, messages: list[dict]) -> bool:
        return (
            _estimate_tokens(messages, self._config.chars_per_token)
            <= self._config.max_tokens
        )

    def stats(self) -> dict:
        return {**self._stats}


def build_jarvis_trimmer(max_tokens: int = 4096) -> ContextWindowTrimmer:
    return ContextWindowTrimmer(
        TrimConfig(max_tokens=max_tokens, strategy=TrimStrategy.FIFO)
    )


def main():
    import sys

    trimmer = build_jarvis_trimmer(max_tokens=200)  # small budget for demo
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        messages = [
            {"role": "system", "content": "You are JARVIS."},
            {"role": "user", "content": "Tell me about GPU cluster setup. " * 10},
            {
                "role": "assistant",
                "content": "The GPU cluster has M1, M2, and OL1 nodes. " * 8,
            },
            {"role": "user", "content": "What about Redis configuration? " * 10},
            {
                "role": "assistant",
                "content": "Redis runs on localhost:6379 with AOF enabled. " * 8,
            },
            {"role": "user", "content": "How do I check GPU temperature?"},
        ]

        print(f"Original: {len(messages)} msgs, ~{_estimate_tokens(messages)} tokens")

        for strategy in TrimStrategy:
            trimmer._config.strategy = strategy
            result = trimmer.trim(messages)
            print(
                f"  {strategy.value:<15} → {result.trimmed_count} msgs, "
                f"~{result.trimmed_tokens} tokens, dropped={result.dropped_count}"
            )

        print(f"\nStats: {json.dumps(trimmer.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(build_jarvis_trimmer().stats(), indent=2))


if __name__ == "__main__":
    main()
