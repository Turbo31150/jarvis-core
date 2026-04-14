#!/usr/bin/env python3
"""JARVIS Context Window — Manage rolling context windows for LLM conversations"""

import redis
import json
import time
from datetime import datetime

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:ctx"

MAX_TOKENS = 8000
RESERVE_TOKENS = 1000  # for response


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def create_window(session_id: str, system_prompt: str = "", max_tokens: int = MAX_TOKENS) -> dict:
    window = {
        "session_id": session_id,
        "system_prompt": system_prompt,
        "max_tokens": max_tokens,
        "used_tokens": estimate_tokens(system_prompt),
        "messages": [],
        "created_at": datetime.now().isoformat()[:19],
    }
    r.setex(f"{PREFIX}:{session_id}", 3600, json.dumps(window))
    return window


def add_message(session_id: str, role: str, content: str) -> dict:
    raw = r.get(f"{PREFIX}:{session_id}")
    if not raw:
        window = create_window(session_id)
    else:
        window = json.loads(raw)

    msg_tokens = estimate_tokens(content)
    available = window["max_tokens"] - RESERVE_TOKENS

    # Evict oldest messages if needed
    while window["used_tokens"] + msg_tokens > available and window["messages"]:
        evicted = window["messages"].pop(0)
        window["used_tokens"] -= estimate_tokens(evicted["content"])

    window["messages"].append({"role": role, "content": content, "tokens": msg_tokens, "ts": time.strftime("%H:%M:%S")})
    window["used_tokens"] += msg_tokens
    r.setex(f"{PREFIX}:{session_id}", 3600, json.dumps(window))
    return {"messages": len(window["messages"]), "used_tokens": window["used_tokens"], "available": available - window["used_tokens"]}


def get_context(session_id: str) -> list:
    raw = r.get(f"{PREFIX}:{session_id}")
    if not raw:
        return []
    window = json.loads(raw)
    ctx = []
    if window.get("system_prompt"):
        ctx.append({"role": "system", "content": window["system_prompt"]})
    ctx.extend({"role": m["role"], "content": m["content"]} for m in window["messages"])
    return ctx


def stats(session_id: str) -> dict:
    raw = r.get(f"{PREFIX}:{session_id}")
    if not raw:
        return {"error": "not found"}
    window = json.loads(raw)
    return {
        "session_id": session_id,
        "messages": len(window["messages"]),
        "used_tokens": window["used_tokens"],
        "max_tokens": window["max_tokens"],
        "pct_full": round(window["used_tokens"] / window["max_tokens"] * 100, 1),
    }


if __name__ == "__main__":
    sid = "test_session"
    create_window(sid, "You are JARVIS, a helpful AI assistant.", max_tokens=500)
    for i in range(5):
        add_message(sid, "user", f"Question {i}: What is {i}+{i}?")
        add_message(sid, "assistant", f"The answer to {i}+{i} is {i*2}.")
    s = stats(sid)
    ctx = get_context(sid)
    print(f"Context window: {s['messages']} msgs, {s['used_tokens']}/{s['max_tokens']} tokens ({s['pct_full']}% full)")
    print(f"Messages in context: {len(ctx)}")
