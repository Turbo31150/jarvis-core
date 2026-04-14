#!/usr/bin/env python3
"""
jarvis_session_store — Persistent multi-turn conversation sessions
Stores history, metadata, agent context per session with TTL management
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.session_store")

REDIS_PREFIX = "jarvis:session:"
INDEX_KEY = "jarvis:sessions:index"
DEFAULT_TTL = 86400  # 24h
MAX_HISTORY = 200  # max turns per session


@dataclass
class Message:
    role: str  # user | assistant | system
    content: str
    ts: float = field(default_factory=time.time)
    tokens: int = 0
    model: str = ""

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "content": self.content,
            "ts": self.ts,
            "tokens": self.tokens,
            "model": self.model,
        }


@dataclass
class Session:
    session_id: str
    agent: str = "jarvis"
    model: str = "qwen/qwen3.5-9b"
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    history: list[Message] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    total_tokens: int = 0
    turn_count: int = 0

    @property
    def age_s(self) -> float:
        return time.time() - self.created_at

    @property
    def idle_s(self) -> float:
        return time.time() - self.last_active

    def add_message(
        self, role: str, content: str, tokens: int = 0, model: str = ""
    ) -> Message:
        msg = Message(
            role=role, content=content, tokens=tokens, model=model or self.model
        )
        self.history.append(msg)
        self.total_tokens += tokens
        if role == "user":
            self.turn_count += 1
        self.last_active = time.time()
        # Trim history
        if len(self.history) > MAX_HISTORY:
            self.history = self.history[-MAX_HISTORY:]
        return msg

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "agent": self.agent,
            "model": self.model,
            "created_at": self.created_at,
            "last_active": self.last_active,
            "turn_count": self.turn_count,
            "total_tokens": self.total_tokens,
            "history": [m.to_dict() for m in self.history],
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Session":
        s = cls(
            session_id=d["session_id"],
            agent=d.get("agent", "jarvis"),
            model=d.get("model", "qwen/qwen3.5-9b"),
            created_at=d.get("created_at", time.time()),
            last_active=d.get("last_active", time.time()),
            total_tokens=d.get("total_tokens", 0),
            turn_count=d.get("turn_count", 0),
            metadata=d.get("metadata", {}),
        )
        for m in d.get("history", []):
            s.history.append(
                Message(
                    role=m["role"],
                    content=m["content"],
                    ts=m.get("ts", 0),
                    tokens=m.get("tokens", 0),
                    model=m.get("model", ""),
                )
            )
        return s

    def openai_messages(self) -> list[dict]:
        """Return history in OpenAI messages format."""
        return [{"role": m.role, "content": m.content} for m in self.history]


class SessionStore:
    def __init__(self, default_ttl: int = DEFAULT_TTL):
        self.redis: aioredis.Redis | None = None
        self._cache: dict[str, Session] = {}
        self.ttl = default_ttl

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    async def create(
        self,
        agent: str = "jarvis",
        model: str = "qwen/qwen3.5-9b",
        metadata: dict | None = None,
        system_prompt: str | None = None,
    ) -> Session:
        sid = str(uuid.uuid4())[:12]
        s = Session(session_id=sid, agent=agent, model=model, metadata=metadata or {})
        if system_prompt:
            s.add_message("system", system_prompt)
        await self._save(s)
        return s

    async def get(self, session_id: str) -> Session | None:
        if session_id in self._cache:
            return self._cache[session_id]
        if self.redis:
            raw = await self.redis.get(f"{REDIS_PREFIX}{session_id}")
            if raw:
                s = Session.from_dict(json.loads(raw))
                self._cache[session_id] = s
                return s
        return None

    async def get_or_create(self, session_id: str | None = None, **kwargs) -> Session:
        if session_id:
            s = await self.get(session_id)
            if s:
                return s
        return await self.create(**kwargs)

    async def _save(self, s: Session):
        self._cache[s.session_id] = s
        if self.redis:
            await self.redis.set(
                f"{REDIS_PREFIX}{s.session_id}",
                json.dumps(s.to_dict()),
                ex=self.ttl,
            )
            await self.redis.zadd(
                INDEX_KEY,
                {s.session_id: s.last_active},
            )

    async def add_turn(
        self,
        session_id: str,
        user_content: str,
        assistant_content: str,
        tokens: int = 0,
        model: str = "",
    ) -> Session | None:
        s = await self.get(session_id)
        if not s:
            return None
        s.add_message("user", user_content)
        s.add_message("assistant", assistant_content, tokens=tokens, model=model)
        await self._save(s)
        return s

    async def delete(self, session_id: str):
        self._cache.pop(session_id, None)
        if self.redis:
            await self.redis.delete(f"{REDIS_PREFIX}{session_id}")
            await self.redis.zrem(INDEX_KEY, session_id)

    async def list_sessions(
        self, agent: str | None = None, limit: int = 50
    ) -> list[dict]:
        if not self.redis:
            sessions = list(self._cache.values())
            if agent:
                sessions = [s for s in sessions if s.agent == agent]
            return [
                s.to_dict()
                for s in sorted(sessions, key=lambda x: -x.last_active)[:limit]
            ]

        # Get recent sessions from sorted set
        ids = await self.redis.zrevrange(INDEX_KEY, 0, limit - 1)
        result = []
        for sid in ids:
            raw = await self.redis.get(f"{REDIS_PREFIX}{sid}")
            if raw:
                d = json.loads(raw)
                if agent and d.get("agent") != agent:
                    continue
                result.append(
                    {
                        "session_id": d["session_id"],
                        "agent": d["agent"],
                        "model": d["model"],
                        "turn_count": d["turn_count"],
                        "total_tokens": d["total_tokens"],
                        "last_active": d["last_active"],
                        "idle_min": round((time.time() - d["last_active"]) / 60, 1),
                    }
                )
        return result

    async def purge_old(self, max_idle_hours: int = 24) -> int:
        cutoff = time.time() - max_idle_hours * 3600
        if not self.redis:
            return 0
        old_ids = await self.redis.zrangebyscore(INDEX_KEY, 0, cutoff)
        for sid in old_ids:
            await self.delete(sid)
        return len(old_ids)


# ── CLI ───────────────────────────────────────────────────────────────────────


async def main():
    import sys

    store = SessionStore()
    await store.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"

    if cmd == "list":
        sessions = await store.list_sessions()
        if not sessions:
            print("No active sessions")
        else:
            print(
                f"{'Session':<14} {'Agent':<12} {'Model':<20} {'Turns':>6} {'Tokens':>8} {'Idle':>8}"
            )
            print("-" * 75)
            for s in sessions:
                print(
                    f"{s['session_id']:<14} {s['agent']:<12} {s['model']:<20} "
                    f"{s['turn_count']:>6} {s['total_tokens']:>8} {s['idle_min']:>6.1f}m"
                )

    elif cmd == "get" and len(sys.argv) > 2:
        s = await store.get(sys.argv[2])
        if s:
            print(json.dumps(s.to_dict(), indent=2))
        else:
            print(f"Session '{sys.argv[2]}' not found")

    elif cmd == "create":
        agent = sys.argv[2] if len(sys.argv) > 2 else "jarvis"
        model = sys.argv[3] if len(sys.argv) > 3 else "qwen/qwen3.5-9b"
        s = await store.create(agent=agent, model=model)
        print(f"Created: {s.session_id}")

    elif cmd == "purge":
        n = await store.purge_old()
        print(f"Purged {n} old sessions")

    elif cmd == "delete" and len(sys.argv) > 2:
        await store.delete(sys.argv[2])
        print(f"Deleted: {sys.argv[2]}")


if __name__ == "__main__":
    asyncio.run(main())
