#!/usr/bin/env python3
"""JARVIS Session Manager — Track active sessions, contexts, conversation history"""

import redis
import json
import uuid
import time
from datetime import datetime

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:session"
SESSION_TTL = 3600  # 1 hour


def create_session(user: str = "turbo", metadata: dict = None) -> str:
    sid = f"sess:{uuid.uuid4().hex[:10]}"
    session = {
        "id": sid,
        "user": user,
        "created_at": datetime.now().isoformat()[:19],
        "last_active": time.time(),
        "metadata": json.dumps(metadata or {}),
        "message_count": 0,
    }
    r.hset(f"{PREFIX}:{sid}", mapping=session)
    r.expire(f"{PREFIX}:{sid}", SESSION_TTL)
    r.sadd(f"{PREFIX}:active", sid)
    return sid


def get_session(sid: str) -> dict:
    data = r.hgetall(f"{PREFIX}:{sid}")
    if not data:
        return {}
    data["metadata"] = json.loads(data.get("metadata", "{}"))
    return data


def add_message(sid: str, role: str, content: str, tokens: int = 0) -> bool:
    if not r.exists(f"{PREFIX}:{sid}"):
        return False
    msg = json.dumps({
        "role": role,
        "content": content[:2000],
        "tokens": tokens,
        "ts": datetime.now().isoformat()[:19],
    })
    r.rpush(f"{PREFIX}:{sid}:messages", msg)
    r.expire(f"{PREFIX}:{sid}:messages", SESSION_TTL)
    r.hincrby(f"{PREFIX}:{sid}", "message_count", 1)
    r.hset(f"{PREFIX}:{sid}", "last_active", time.time())
    r.expire(f"{PREFIX}:{sid}", SESSION_TTL)
    return True


def get_history(sid: str, last_n: int = 20) -> list:
    raw = r.lrange(f"{PREFIX}:{sid}:messages", -last_n, -1)
    return [json.loads(m) for m in raw]


def close_session(sid: str):
    r.srem(f"{PREFIX}:active", sid)
    r.expire(f"{PREFIX}:{sid}", 300)  # keep 5 min for audit
    r.expire(f"{PREFIX}:{sid}:messages", 300)


def active_sessions() -> list:
    sids = r.smembers(f"{PREFIX}:active")
    result = []
    for sid in sids:
        data = r.hgetall(f"{PREFIX}:{sid}")
        if data:
            result.append({
                "id": sid,
                "user": data.get("user"),
                "messages": int(data.get("message_count", 0)),
                "created": data.get("created_at"),
            })
    return result


def stats() -> dict:
    active = r.scard(f"{PREFIX}:active")
    return {"active_sessions": active, "sessions": active_sessions()}


if __name__ == "__main__":
    sid = create_session("turbo", {"source": "test"})
    print(f"Created: {sid}")
    add_message(sid, "user", "Bonjour JARVIS")
    add_message(sid, "assistant", "Bonjour ! Comment puis-je vous aider ?")
    hist = get_history(sid)
    print(f"History ({len(hist)} messages):")
    for m in hist:
        print(f"  [{m['role']}] {m['content'][:60]}")
    print(f"Stats: {stats()}")
    close_session(sid)
    print(f"Closed. Active: {stats()['active_sessions']}")
