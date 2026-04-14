#!/usr/bin/env python3
"""JARVIS Context Tracker — Track conversation context and token budgets per session"""

import redis
import json
import time

r = redis.Redis(decode_responses=True)

SESSION_PREFIX = "jarvis:ctx:"
ACTIVE_KEY = "jarvis:ctx:active_sessions"

# Token cost estimates (per 1k tokens)
COSTS = {
    "m1": 0.0,
    "m2": 0.0,
    "m32": 0.0,
    "ol1": 0.0,
    "claude": 0.015,
    "gemini": 0.0007,
}
MAX_TOKENS = 8192


def _sid_key(session_id: str) -> str:
    return f"{SESSION_PREFIX}{session_id}"


def create_session(
    session_id: str, model: str = "m2", max_tokens: int = MAX_TOKENS
) -> dict:
    ctx = {
        "id": session_id,
        "model": model,
        "max_tokens": max_tokens,
        "used_tokens": 0,
        "messages": [],
        "created_at": time.time(),
        "last_active": time.time(),
        "cost_usd": 0.0,
    }
    r.setex(_sid_key(session_id), 7200, json.dumps(ctx))
    r.sadd(ACTIVE_KEY, session_id)
    return ctx


def add_message(session_id: str, role: str, content: str, tokens: int = None) -> dict:
    raw = r.get(_sid_key(session_id))
    if not raw:
        ctx = create_session(session_id)
    else:
        ctx = json.loads(raw)

    if tokens is None:
        tokens = len(content.split()) * 4 // 3  # rough estimate

    msg = {"role": role, "content": content[:2000], "tokens": tokens, "ts": time.time()}
    ctx["messages"].append(msg)
    ctx["used_tokens"] += tokens
    ctx["last_active"] = time.time()

    # Cost tracking
    rate = COSTS.get(ctx["model"], 0.0)
    ctx["cost_usd"] += (tokens / 1000) * rate

    # Trim context if over budget (keep system + last N messages)
    while ctx["used_tokens"] > ctx["max_tokens"] and len(ctx["messages"]) > 2:
        removed = ctx["messages"].pop(1)  # remove oldest non-system
        ctx["used_tokens"] -= removed["tokens"]

    r.setex(_sid_key(session_id), 7200, json.dumps(ctx))
    return {
        "tokens_used": ctx["used_tokens"],
        "budget_pct": round(ctx["used_tokens"] / ctx["max_tokens"] * 100),
    }


def get_context(session_id: str, last_n: int = None) -> list:
    raw = r.get(_sid_key(session_id))
    if not raw:
        return []
    ctx = json.loads(raw)
    msgs = ctx["messages"]
    return msgs[-last_n:] if last_n else msgs


def get_session(session_id: str) -> dict:
    raw = r.get(_sid_key(session_id))
    return json.loads(raw) if raw else {}


def close_session(session_id: str):
    r.delete(_sid_key(session_id))
    r.srem(ACTIVE_KEY, session_id)


def stats() -> dict:
    active_ids = r.smembers(ACTIVE_KEY)
    sessions = []
    for sid in active_ids:
        raw = r.get(_sid_key(sid))
        if raw:
            ctx = json.loads(raw)
            sessions.append(
                {
                    "id": sid,
                    "model": ctx["model"],
                    "tokens": ctx["used_tokens"],
                    "messages": len(ctx["messages"]),
                    "budget_pct": round(ctx["used_tokens"] / ctx["max_tokens"] * 100),
                }
            )
        else:
            r.srem(ACTIVE_KEY, sid)
    return {"active_sessions": len(sessions), "sessions": sessions}


if __name__ == "__main__":
    sid = "test_session_001"
    create_session(sid, model="m32")
    add_message(sid, "system", "You are JARVIS, a helpful AI cluster assistant.")
    add_message(sid, "user", "What is the current GPU temperature?")
    info = add_message(
        sid, "assistant", "GPU0 is at 36°C, all GPUs within normal range."
    )
    print(
        f"Session: {sid} | {info['tokens_used']} tokens ({info['budget_pct']}% budget)"
    )
    ctx = get_context(sid)
    print(f"Messages: {len(ctx)}")
    for m in ctx:
        print(f"  [{m['role']}] {m['content'][:60]}")
    print(f"Stats: {stats()}")
    close_session(sid)
