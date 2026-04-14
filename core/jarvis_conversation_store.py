#!/usr/bin/env python3
"""JARVIS Conversation Store — Persistent multi-session conversation history with search"""

import redis
import json
import time
import hashlib

r = redis.Redis(decode_responses=True)

SESSION_PREFIX = "jarvis:conv:session:"
MSG_PREFIX = "jarvis:conv:msg:"
INDEX_KEY = "jarvis:conv:sessions"
STATS_KEY = "jarvis:conv:stats"
SEARCH_INDEX = "jarvis:conv:search:"


def new_session(identity: str = "default", metadata: dict = None) -> str:
    sid = hashlib.md5(f"{identity}:{time.time()}".encode()).hexdigest()[:12]
    session = {
        "id": sid,
        "identity": identity,
        "metadata": metadata or {},
        "created_at": time.time(),
        "updated_at": time.time(),
        "msg_count": 0,
    }
    r.setex(f"{SESSION_PREFIX}{sid}", 86400 * 30, json.dumps(session))
    r.zadd(INDEX_KEY, {sid: time.time()})
    r.hincrby(STATS_KEY, "sessions_created", 1)
    return sid


def add_message(sid: str, role: str, content: str, metadata: dict = None) -> str:
    mid = hashlib.md5(f"{sid}:{time.time()}:{role}".encode()).hexdigest()[:12]
    msg = {
        "id": mid,
        "session_id": sid,
        "role": role,
        "content": content,
        "metadata": metadata or {},
        "ts": time.time(),
    }
    r.setex(f"{MSG_PREFIX}{mid}", 86400 * 30, json.dumps(msg))
    r.rpush(f"{SESSION_PREFIX}{sid}:msgs", mid)
    r.expire(f"{SESSION_PREFIX}{sid}:msgs", 86400 * 30)

    # Update session
    raw = r.get(f"{SESSION_PREFIX}{sid}")
    if raw:
        session = json.loads(raw)
        session["updated_at"] = time.time()
        session["msg_count"] = session.get("msg_count", 0) + 1
        r.setex(f"{SESSION_PREFIX}{sid}", 86400 * 30, json.dumps(session))

    # Inverted index for search (trigrams)
    words = content.lower().split()
    for word in set(words[:50]):
        if len(word) >= 3:
            r.sadd(f"{SEARCH_INDEX}{word[:6]}", mid)
            r.expire(f"{SEARCH_INDEX}{word[:6]}", 86400 * 7)

    r.hincrby(STATS_KEY, "messages_added", 1)
    return mid


def get_history(sid: str, limit: int = 50) -> list:
    mids = r.lrange(f"{SESSION_PREFIX}{sid}:msgs", -limit, -1)
    msgs = []
    for mid in mids:
        raw = r.get(f"{MSG_PREFIX}{mid}")
        if raw:
            msgs.append(json.loads(raw))
    return msgs


def get_messages_for_llm(sid: str, limit: int = 20) -> list:
    """Return history in LLM format [{role, content}]."""
    history = get_history(sid, limit)
    return [{"role": m["role"], "content": m["content"]} for m in history]


def search_messages(query: str, limit: int = 10) -> list:
    words = query.lower().split()
    if not words:
        return []
    # Find candidate message IDs from first word
    candidates = r.smembers(f"{SEARCH_INDEX}{words[0][:6]}")
    for word in words[1:]:
        more = r.smembers(f"{SEARCH_INDEX}{word[:6]}")
        candidates = candidates & more if candidates else more
    results = []
    for mid in list(candidates)[: limit * 2]:
        raw = r.get(f"{MSG_PREFIX}{mid}")
        if raw:
            msg = json.loads(raw)
            # Simple relevance: count word matches
            score = sum(1 for w in words if w in msg["content"].lower())
            results.append((score, msg))
    results.sort(key=lambda x: -x[0])
    return [m for _, m in results[:limit]]


def list_sessions(identity: str = None, limit: int = 20) -> list:
    sids = r.zrevrange(INDEX_KEY, 0, limit * 3 - 1)
    sessions = []
    for sid in sids:
        raw = r.get(f"{SESSION_PREFIX}{sid}")
        if raw:
            s = json.loads(raw)
            if identity is None or s.get("identity") == identity:
                sessions.append(s)
        if len(sessions) >= limit:
            break
    return sessions


def delete_session(sid: str) -> bool:
    mids = r.lrange(f"{SESSION_PREFIX}{sid}:msgs", 0, -1)
    for mid in mids:
        r.delete(f"{MSG_PREFIX}{mid}")
    r.delete(f"{SESSION_PREFIX}{sid}:msgs")
    r.delete(f"{SESSION_PREFIX}{sid}")
    r.zrem(INDEX_KEY, sid)
    return True


def stats() -> dict:
    s = r.hgetall(STATS_KEY)
    total_sessions = r.zcard(INDEX_KEY)
    return {"total_sessions": total_sessions, **{k: int(v) for k, v in s.items()}}


if __name__ == "__main__":
    sid = new_session("turbo", {"source": "test"})
    add_message(sid, "user", "Comment va le cluster GPU aujourd'hui ?")
    add_message(sid, "assistant", "M2 est instable, M32 répond bien à 115ms. OL1 OK.")
    add_message(sid, "user", "Lance un warmup sur M32 s'il te plaît.")
    add_message(
        sid, "assistant", "Warmup M32 mistral-7b: 115ms ✅, phi-3.1: 11s (cold start)."
    )

    history = get_messages_for_llm(sid)
    print(f"Session {sid}: {len(history)} messages")
    for m in history:
        print(f"  [{m['role']:9s}] {m['content'][:60]}")

    results = search_messages("cluster GPU")
    print(f"\nSearch 'cluster GPU': {len(results)} results")
    for r_ in results:
        print(f"  [{r_['role']}] {r_['content'][:60]}")

    print(f"\nStats: {stats()}")
