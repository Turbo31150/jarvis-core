#!/usr/bin/env python3
"""
jarvis_context_assembler — Assembles LLM context window from multiple sources
System prompt, memory, RAG chunks, conversation history, injected facts
"""

import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

log = logging.getLogger("jarvis.context_assembler")

# Approximate chars per token (GPT-4 / Qwen)
CHARS_PER_TOKEN = 3.8


def _estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / CHARS_PER_TOKEN))


class ContextSlot(str, Enum):
    SYSTEM = "system"  # system prompt / persona
    MEMORY = "memory"  # long-term memory facts
    RAG = "rag"  # retrieved document chunks
    HISTORY = "history"  # conversation turns
    TASK = "task"  # current task / instruction
    EXAMPLES = "examples"  # few-shot examples
    TOOLS = "tools"  # tool schemas
    FACTS = "facts"  # injected structured facts
    SAFETY = "safety"  # safety / guardrail instructions
    CUSTOM = "custom"  # user-defined slot


class TrimStrategy(str, Enum):
    DROP_OLDEST = "drop_oldest"  # remove oldest turns/chunks
    DROP_LOWEST = "drop_lowest"  # drop lowest-priority items
    TRUNCATE = "truncate"  # cut text at token limit
    SUMMARIZE = "summarize"  # (hook: caller provides summary fn)


@dataclass
class ContextItem:
    slot: ContextSlot
    content: str
    priority: int = 50  # 0 (drop first) → 100 (keep always)
    label: str = ""  # optional display label
    ts: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def token_count(self) -> int:
        return _estimate_tokens(self.content)

    def to_message(self, role: str = "system") -> dict:
        if self.label:
            return {"role": role, "content": f"[{self.label}]\n{self.content}"}
        return {"role": role, "content": self.content}


@dataclass
class AssemblyResult:
    messages: list[dict]
    items_included: list[ContextItem]
    items_dropped: list[ContextItem]
    total_tokens: int
    token_budget: int
    strategy_used: TrimStrategy
    assembly_ms: float = 0.0

    @property
    def utilization(self) -> float:
        return self.total_tokens / max(self.token_budget, 1)

    def to_dict(self) -> dict:
        return {
            "message_count": len(self.messages),
            "total_tokens": self.total_tokens,
            "token_budget": self.token_budget,
            "utilization": round(self.utilization, 3),
            "items_included": len(self.items_included),
            "items_dropped": len(self.items_dropped),
            "strategy": self.strategy_used.value,
            "assembly_ms": round(self.assembly_ms, 2),
        }


# Default slot ordering and roles
_SLOT_ORDER: list[ContextSlot] = [
    ContextSlot.SYSTEM,
    ContextSlot.SAFETY,
    ContextSlot.MEMORY,
    ContextSlot.FACTS,
    ContextSlot.EXAMPLES,
    ContextSlot.RAG,
    ContextSlot.TOOLS,
    ContextSlot.HISTORY,
    ContextSlot.TASK,
    ContextSlot.CUSTOM,
]

_SLOT_ROLE: dict[ContextSlot, str] = {
    ContextSlot.SYSTEM: "system",
    ContextSlot.SAFETY: "system",
    ContextSlot.MEMORY: "system",
    ContextSlot.FACTS: "system",
    ContextSlot.EXAMPLES: "user",
    ContextSlot.RAG: "system",
    ContextSlot.TOOLS: "system",
    ContextSlot.HISTORY: "user",  # history items have their own role in metadata
    ContextSlot.TASK: "user",
    ContextSlot.CUSTOM: "user",
}


class ContextAssembler:
    """
    Assembles a list of OpenAI-compatible messages from prioritized context items,
    respecting a token budget with configurable trim strategy.
    """

    def __init__(
        self,
        token_budget: int = 4096,
        trim_strategy: TrimStrategy = TrimStrategy.DROP_OLDEST,
        slot_order: list[ContextSlot] | None = None,
        reserve_for_response: int = 512,
    ):
        self._token_budget = token_budget
        self._trim_strategy = trim_strategy
        self._slot_order = slot_order or _SLOT_ORDER
        self._reserve = reserve_for_response
        self._items: list[ContextItem] = []
        self._summarize_fn = None  # optional: fn(items) -> str
        self._stats: dict[str, int] = {
            "assemblies": 0,
            "items_added": 0,
            "items_dropped_total": 0,
        }

    @property
    def effective_budget(self) -> int:
        return self._token_budget - self._reserve

    def set_summarize_fn(self, fn):
        """Set a function(list[ContextItem]) -> str for SUMMARIZE strategy."""
        self._summarize_fn = fn

    def add(self, item: ContextItem):
        self._items.append(item)
        self._stats["items_added"] += 1

    def add_system(self, content: str, priority: int = 100, label: str = ""):
        self.add(ContextItem(ContextSlot.SYSTEM, content, priority, label))

    def add_safety(self, content: str):
        self.add(ContextItem(ContextSlot.SAFETY, content, priority=100, label="Safety"))

    def add_memory(self, facts: list[str], priority: int = 70):
        if not facts:
            return
        content = "\n".join(f"- {f}" for f in facts)
        self.add(ContextItem(ContextSlot.MEMORY, content, priority, label="Memory"))

    def add_rag(self, chunks: list[str], priority: int = 60):
        for i, chunk in enumerate(chunks):
            self.add(
                ContextItem(
                    ContextSlot.RAG,
                    chunk,
                    priority,
                    label=f"Context[{i + 1}]",
                    ts=time.time() + i,  # preserve insertion order
                )
            )

    def add_history(self, turns: list[dict], priority: int = 50):
        """Each turn: {"role": "user"|"assistant", "content": "..."}"""
        for i, turn in enumerate(turns):
            self.add(
                ContextItem(
                    ContextSlot.HISTORY,
                    turn.get("content", ""),
                    priority,
                    metadata={"role": turn.get("role", "user")},
                    ts=time.time() + i,
                )
            )

    def add_task(self, task: str, priority: int = 90):
        self.add(ContextItem(ContextSlot.TASK, task, priority, label="Task"))

    def add_examples(self, examples: list[dict], priority: int = 40):
        if not examples:
            return
        lines = []
        for ex in examples:
            lines.append(f"User: {ex.get('input', '')}")
            lines.append(f"Assistant: {ex.get('output', '')}")
        self.add(
            ContextItem(
                ContextSlot.EXAMPLES, "\n".join(lines), priority, label="Examples"
            )
        )

    def add_facts(self, facts: dict[str, Any], priority: int = 75):
        if not facts:
            return
        lines = [f"{k}: {v}" for k, v in facts.items()]
        self.add(
            ContextItem(ContextSlot.FACTS, "\n".join(lines), priority, label="Facts")
        )

    def add_tools(self, schemas: list[dict], priority: int = 80):
        if not schemas:
            return
        content = json.dumps(schemas, indent=2)
        self.add(ContextItem(ContextSlot.TOOLS, content, priority, label="Tools"))

    def clear(self, slot: ContextSlot | None = None):
        if slot is None:
            self._items.clear()
        else:
            self._items = [i for i in self._items if i.slot != slot]

    def _sort_items(self, items: list[ContextItem]) -> list[ContextItem]:
        """Sort by slot order, then by ts within each slot."""
        slot_rank = {s: i for i, s in enumerate(self._slot_order)}
        return sorted(items, key=lambda it: (slot_rank.get(it.slot, 99), it.ts))

    def assemble(
        self,
        extra_items: list[ContextItem] | None = None,
        token_budget: int | None = None,
    ) -> AssemblyResult:
        t0 = time.time()
        self._stats["assemblies"] += 1

        budget = (token_budget or self._token_budget) - self._reserve
        all_items = self._items + (extra_items or [])
        sorted_items = self._sort_items(all_items)

        included: list[ContextItem] = []
        dropped: list[ContextItem] = []
        used_tokens = 0

        if self._trim_strategy == TrimStrategy.DROP_LOWEST:
            # Sort by priority descending, fill greedily
            by_priority = sorted(sorted_items, key=lambda it: -it.priority)
            accepted_ids = set()
            for item in by_priority:
                if used_tokens + item.token_count <= budget:
                    accepted_ids.add(id(item))
                    used_tokens += item.token_count
            # Rebuild in slot order
            for item in sorted_items:
                if id(item) in accepted_ids:
                    included.append(item)
                else:
                    dropped.append(item)

        elif self._trim_strategy == TrimStrategy.DROP_OLDEST:
            # Fill from highest-priority slots; within HISTORY drop oldest first
            # Separate history from rest
            non_history = [i for i in sorted_items if i.slot != ContextSlot.HISTORY]
            history = [i for i in sorted_items if i.slot == ContextSlot.HISTORY]

            # Include all non-history first (by priority)
            for item in sorted(non_history, key=lambda i: -i.priority):
                if used_tokens + item.token_count <= budget:
                    included.append(item)
                    used_tokens += item.token_count
                else:
                    dropped.append(item)

            # Fill remaining budget with history (newest first)
            for item in reversed(history):
                if used_tokens + item.token_count <= budget:
                    included.append(item)
                    used_tokens += item.token_count
                else:
                    dropped.append(item)

            # Re-sort included by slot order
            included = self._sort_items(included)

        elif self._trim_strategy == TrimStrategy.TRUNCATE:
            for item in sorted_items:
                remaining = budget - used_tokens
                if remaining <= 0:
                    dropped.append(item)
                    continue
                if item.token_count <= remaining:
                    included.append(item)
                    used_tokens += item.token_count
                else:
                    # Truncate content
                    max_chars = int(remaining * CHARS_PER_TOKEN)
                    truncated = ContextItem(
                        slot=item.slot,
                        content=item.content[:max_chars] + "…",
                        priority=item.priority,
                        label=item.label,
                        ts=item.ts,
                        metadata=item.metadata,
                    )
                    included.append(truncated)
                    used_tokens += truncated.token_count
                    dropped.append(item)

        elif self._trim_strategy == TrimStrategy.SUMMARIZE and self._summarize_fn:
            # Try to fit; if over budget, summarize overflow and retry
            for item in sorted_items:
                if used_tokens + item.token_count <= budget:
                    included.append(item)
                    used_tokens += item.token_count
                else:
                    dropped.append(item)

            if dropped and self._summarize_fn:
                summary_text = self._summarize_fn(dropped)
                summary_item = ContextItem(
                    ContextSlot.MEMORY, summary_text, 60, label="Summary"
                )
                if used_tokens + summary_item.token_count <= budget:
                    included.append(summary_item)
                    used_tokens += summary_item.token_count

        else:
            # Fallback: fit greedily
            for item in sorted_items:
                if used_tokens + item.token_count <= budget:
                    included.append(item)
                    used_tokens += item.token_count
                else:
                    dropped.append(item)

        self._stats["items_dropped_total"] += len(dropped)

        # Build messages
        messages = self._build_messages(included)

        return AssemblyResult(
            messages=messages,
            items_included=included,
            items_dropped=dropped,
            total_tokens=used_tokens,
            token_budget=token_budget or self._token_budget,
            strategy_used=self._trim_strategy,
            assembly_ms=(time.time() - t0) * 1000,
        )

    def _build_messages(self, items: list[ContextItem]) -> list[dict]:
        messages: list[dict] = []
        current_system_parts: list[str] = []

        def flush_system():
            if current_system_parts:
                messages.append(
                    {
                        "role": "system",
                        "content": "\n\n".join(current_system_parts),
                    }
                )
                current_system_parts.clear()

        for item in items:
            role = item.metadata.get("role") or _SLOT_ROLE.get(item.slot, "user")

            if role == "system":
                part = f"[{item.label}]\n{item.content}" if item.label else item.content
                current_system_parts.append(part)
            else:
                flush_system()
                content = (
                    f"[{item.label}]\n{item.content}" if item.label else item.content
                )
                messages.append({"role": role, "content": content})

        flush_system()
        return messages

    def stats(self) -> dict:
        return {
            **self._stats,
            "current_items": len(self._items),
            "current_tokens": sum(i.token_count for i in self._items),
            "token_budget": self._token_budget,
        }


def build_jarvis_context_assembler(
    token_budget: int = 8192,
    strategy: TrimStrategy = TrimStrategy.DROP_OLDEST,
) -> ContextAssembler:
    return ContextAssembler(
        token_budget=token_budget,
        trim_strategy=strategy,
        reserve_for_response=1024,
    )


def main():
    import sys

    ca = build_jarvis_context_assembler(token_budget=2048)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Context assembler demo...\n")

        ca.add_system(
            "You are JARVIS, an AI cluster orchestration assistant.",
            priority=100,
        )
        ca.add_safety("Never reveal credentials. Always verify destructive operations.")
        ca.add_memory(
            [
                "User prefers concise responses.",
                "Cluster M1 is the primary node.",
                "Trading pipeline uses MEXC futures.",
            ]
        )
        ca.add_facts(
            {
                "cluster_status": "healthy",
                "active_models": 4,
                "gpu_temp_max": "68°C",
            }
        )
        ca.add_history(
            [
                {"role": "user", "content": "What models are running on M1?"},
                {
                    "role": "assistant",
                    "content": "M1 is running qwen3.5-9b and deepseek-r1.",
                },
                {"role": "user", "content": "Can you check GPU temps?"},
                {"role": "assistant", "content": "GPU temps are all below 70°C."},
            ]
        )
        ca.add_task("List all healthy nodes in the cluster.")

        result = ca.assemble()

        print(f"  Messages: {len(result.messages)}")
        print(f"  Tokens: {result.total_tokens}/{result.token_budget}")
        print(f"  Utilization: {result.utilization:.1%}")
        print(f"  Dropped: {len(result.items_dropped)} items")
        print(f"  Strategy: {result.strategy_used.value}")
        print()
        for msg in result.messages:
            preview = msg["content"][:80].replace("\n", " ")
            print(f"  [{msg['role']:10}] {preview}")

        print(f"\nAssembly info: {json.dumps(result.to_dict(), indent=2)}")
        print(f"\nStats: {json.dumps(ca.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(ca.stats(), indent=2))


if __name__ == "__main__":
    main()
