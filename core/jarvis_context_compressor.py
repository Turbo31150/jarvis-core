#!/usr/bin/env python3
"""JARVIS Context Compressor — Compress long conversation history to fit token budgets"""

import redis
import re

r = redis.Redis(decode_responses=True)
STATS_KEY = "jarvis:ctx_compress:stats"


def _token_count(text: str) -> int:
    return len(text.split()) * 4 // 3


def _extract_key_facts(messages: list) -> list:
    """Extract key facts/decisions from a message list."""
    facts = []
    for msg in messages:
        content = msg.get("content", "")
        role = msg.get("role", "user")
        # Extract lines that look like decisions or facts
        for line in content.splitlines():
            line = line.strip()
            if not line or len(line) < 10:
                continue
            # Heuristic: numbered lists, bullet points, definitions, conclusions
            if re.match(r"^(\d+\.|[-•*]|\*\*|=>|→)", line):
                facts.append({"role": role, "fact": line[:200]})
            elif re.search(
                r"\b(result|conclusion|answer|decision|status|error|warning)\b",
                line,
                re.IGNORECASE,
            ):
                facts.append({"role": role, "fact": line[:200]})
    return facts[:20]


def summarize_messages(messages: list) -> str:
    """Create a compact summary of a list of messages."""
    if not messages:
        return ""
    turns = []
    for msg in messages:
        role = msg.get("role", "?")
        content = msg.get("content", "")
        # Truncate each message
        short = content[:150].replace("\n", " ")
        if len(content) > 150:
            short += "…"
        turns.append(f"[{role}] {short}")
    return " | ".join(turns)


def compress(
    messages: list, target_tokens: int = 2000, strategy: str = "sliding_window"
) -> dict:
    """
    Compress a conversation to fit within target_tokens.
    Strategies:
    - sliding_window: keep system + last N messages
    - summarize_old: keep system + summary of old + last N messages
    - key_facts: keep system + extracted facts + last N messages
    """
    total_tokens = sum(_token_count(m.get("content", "")) for m in messages)
    if total_tokens <= target_tokens:
        return {
            "messages": messages,
            "original_tokens": total_tokens,
            "compressed_tokens": total_tokens,
            "strategy": "none",
        }

    system_msgs = [m for m in messages if m.get("role") == "system"]
    non_system = [m for m in messages if m.get("role") != "system"]

    system_tokens = sum(_token_count(m.get("content", "")) for m in system_msgs)
    budget = target_tokens - system_tokens - 200  # reserve for summary/facts

    if strategy == "sliding_window":
        kept = []
        tokens_used = 0
        for msg in reversed(non_system):
            t = _token_count(msg.get("content", ""))
            if tokens_used + t <= budget:
                kept.insert(0, msg)
                tokens_used += t
            else:
                break
        result_msgs = system_msgs + kept

    elif strategy == "summarize_old":
        # Keep last 6 messages, summarize the rest
        recent = non_system[-6:]
        old = non_system[:-6]
        if old:
            summary_text = summarize_messages(old)
            summary_msg = {
                "role": "system",
                "content": f"[Previous conversation summary]: {summary_text}",
                "compressed": True,
            }
            result_msgs = system_msgs + [summary_msg] + recent
        else:
            result_msgs = system_msgs + recent

    elif strategy == "key_facts":
        recent = non_system[-4:]
        old = non_system[:-4]
        facts = _extract_key_facts(old)
        if facts:
            facts_text = "\n".join(f"- {f['fact']}" for f in facts)
            facts_msg = {
                "role": "system",
                "content": f"[Key facts from earlier]:\n{facts_text}",
                "compressed": True,
            }
            result_msgs = system_msgs + [facts_msg] + recent
        else:
            result_msgs = system_msgs + recent
    else:
        result_msgs = messages

    compressed_tokens = sum(_token_count(m.get("content", "")) for m in result_msgs)
    r.hincrby(STATS_KEY, "compressions", 1)
    r.hincrby(STATS_KEY, "tokens_saved", max(0, total_tokens - compressed_tokens))

    return {
        "messages": result_msgs,
        "original_tokens": total_tokens,
        "compressed_tokens": compressed_tokens,
        "saved_tokens": total_tokens - compressed_tokens,
        "reduction_pct": round(
            (total_tokens - compressed_tokens) / max(total_tokens, 1) * 100, 1
        ),
        "strategy": strategy,
    }


def stats() -> dict:
    s = r.hgetall(STATS_KEY)
    return {k: int(v) for k, v in s.items()}


if __name__ == "__main__":
    # Simulate a long conversation
    msgs = [{"role": "system", "content": "You are JARVIS, a cluster AI assistant."}]
    for i in range(20):
        msgs.append(
            {
                "role": "user",
                "content": f"Question {i}: What is the status of GPU{i % 5}?",
            }
        )
        msgs.append(
            {
                "role": "assistant",
                "content": f"GPU{i % 5} is at {30 + i}°C, VRAM usage is {10 + i * 2}%. All systems nominal. The cluster is running smoothly with no issues detected at this time.",
            }
        )

    for strategy in ["sliding_window", "summarize_old", "key_facts"]:
        result = compress(msgs, target_tokens=800, strategy=strategy)
        print(
            f"  [{strategy:16s}] {result['original_tokens']}→{result['compressed_tokens']}t "
            f"(-{result['reduction_pct']}%) | {len(result['messages'])} messages"
        )

    print(f"\nStats: {stats()}")
