#!/usr/bin/env python3
"""JARVIS Conversation Memory — Short-term memory for ongoing conversations"""
import redis, json, hashlib
from datetime import datetime

r = redis.Redis(decode_responses=True)
TTL = 3600  # 1h conversation window
MAX_TURNS = 20

def _key(session_id: str) -> str:
    return f"jarvis:conv:{session_id}"

def add_turn(session_id: str, role: str, content: str, metadata: dict = {}):
    key = _key(session_id)
    turn = {"role": role, "content": content[:1000],
            "ts": datetime.now().isoformat()[:19], **metadata}
    r.rpush(key, json.dumps(turn))
    r.ltrim(key, -MAX_TURNS, -1)  # keep last N turns
    r.expire(key, TTL)

def get_history(session_id: str, last_n: int = 10) -> list:
    key = _key(session_id)
    raw = r.lrange(key, -last_n, -1)
    return [json.loads(t) for t in raw]

def get_context_string(session_id: str, last_n: int = 5) -> str:
    history = get_history(session_id, last_n)
    lines = []
    for turn in history:
        role = turn["role"].upper()
        lines.append(f"{role}: {turn['content'][:200]}")
    return "\n".join(lines)

def summarize_session(session_id: str) -> dict:
    history = get_history(session_id, MAX_TURNS)
    user_turns = [t for t in history if t["role"] == "user"]
    intents = []
    for t in user_turns:
        try:
            from jarvis_intent_classifier import classify
            c = classify(t["content"])
            if c["intent"] != "general":
                intents.append(c["intent"])
        except: pass
    
    from collections import Counter
    intent_counts = Counter(intents)
    return {
        "session_id": session_id,
        "turns": len(history),
        "user_turns": len(user_turns),
        "top_intents": intent_counts.most_common(3),
        "ts": datetime.now().isoformat()[:19]
    }

if __name__ == "__main__":
    sid = "test_session_001"
    add_turn(sid, "user", "analyze BTC long signal")
    add_turn(sid, "assistant", "Looking at BTC charts...")
    add_turn(sid, "user", "check GPU temperature")
    
    history = get_history(sid)
    print(f"History: {len(history)} turns")
    print("Context:\n" + get_context_string(sid))
    print("Summary:", summarize_session(sid))
