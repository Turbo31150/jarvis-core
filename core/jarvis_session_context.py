#!/usr/bin/env python3
"""
jarvis_session_context — Per-session context lifecycle management
Scoped memory, TTL, serialization, cross-session inheritance, snapshots
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.session_context")

REDIS_PREFIX = "jarvis:session:"


class SessionState(str, Enum):
    ACTIVE = "active"
    IDLE = "idle"  # no activity for > idle_timeout
    SUSPENDED = "suspended"  # explicitly paused
    EXPIRED = "expired"
    CLOSED = "closed"


class ContextScope(str, Enum):
    LOCAL = "local"  # this session only
    USER = "user"  # shared across user's sessions
    GLOBAL = "global"  # shared across all sessions


@dataclass
class ContextVar:
    key: str
    value: Any
    scope: ContextScope = ContextScope.LOCAL
    ttl_s: float = 0.0  # 0 = inherit session TTL
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    access_count: int = 0

    def touch(self):
        self.updated_at = time.time()
        self.access_count += 1

    def is_expired(self, session_ttl: float) -> bool:
        ttl = self.ttl_s if self.ttl_s > 0 else session_ttl
        if ttl <= 0:
            return False
        return (time.time() - self.created_at) > ttl

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "value": self.value,
            "scope": self.scope.value,
            "ttl_s": self.ttl_s,
            "access_count": self.access_count,
        }


@dataclass
class SessionSnapshot:
    session_id: str
    ts: float
    state: SessionState
    vars: dict[str, Any]
    metadata: dict[str, Any]

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "ts": self.ts,
            "state": self.state.value,
            "var_count": len(self.vars),
            "vars": self.vars,
            "metadata": self.metadata,
        }


@dataclass
class Session:
    session_id: str
    user_id: str = ""
    state: SessionState = SessionState.ACTIVE
    ttl_s: float = 3600.0
    idle_timeout_s: float = 300.0
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    _vars: dict[str, ContextVar] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    snapshots: list[SessionSnapshot] = field(default_factory=list)
    max_snapshots: int = 10

    def touch(self):
        self.last_activity = time.time()
        if self.state == SessionState.IDLE:
            self.state = SessionState.ACTIVE

    def is_expired(self) -> bool:
        if self.state == SessionState.EXPIRED:
            return True
        if self.ttl_s > 0 and (time.time() - self.created_at) > self.ttl_s:
            return True
        return False

    def is_idle(self) -> bool:
        return (time.time() - self.last_activity) > self.idle_timeout_s

    def set(
        self,
        key: str,
        value: Any,
        scope: ContextScope = ContextScope.LOCAL,
        ttl_s: float = 0.0,
    ):
        if key in self._vars:
            v = self._vars[key]
            v.value = value
            v.touch()
        else:
            self._vars[key] = ContextVar(key=key, value=value, scope=scope, ttl_s=ttl_s)
        self.touch()

    def get(self, key: str, default: Any = None) -> Any:
        v = self._vars.get(key)
        if v is None:
            return default
        if v.is_expired(self.ttl_s):
            del self._vars[key]
            return default
        v.touch()
        return v.value

    def delete(self, key: str) -> bool:
        if key in self._vars:
            del self._vars[key]
            return True
        return False

    def keys(self, scope: ContextScope | None = None) -> list[str]:
        return [
            k
            for k, v in self._vars.items()
            if (scope is None or v.scope == scope) and not v.is_expired(self.ttl_s)
        ]

    def purge_expired(self) -> int:
        expired = [k for k, v in self._vars.items() if v.is_expired(self.ttl_s)]
        for k in expired:
            del self._vars[k]
        return len(expired)

    def snapshot(self) -> SessionSnapshot:
        snap = SessionSnapshot(
            session_id=self.session_id,
            ts=time.time(),
            state=self.state,
            vars={k: v.value for k, v in self._vars.items()},
            metadata=dict(self.metadata),
        )
        self.snapshots.append(snap)
        if len(self.snapshots) > self.max_snapshots:
            self.snapshots.pop(0)
        return snap

    def restore_snapshot(self, index: int = -1) -> bool:
        if not self.snapshots:
            return False
        snap = self.snapshots[index]
        self._vars = {k: ContextVar(key=k, value=v) for k, v in snap.vars.items()}
        self.metadata = dict(snap.metadata)
        return True

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "state": self.state.value,
            "age_s": round(time.time() - self.created_at, 1),
            "idle_s": round(time.time() - self.last_activity, 1),
            "var_count": len(self._vars),
            "ttl_s": self.ttl_s,
            "snapshot_count": len(self.snapshots),
        }

    def serialize(self) -> dict:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "state": self.state.value,
            "ttl_s": self.ttl_s,
            "idle_timeout_s": self.idle_timeout_s,
            "created_at": self.created_at,
            "last_activity": self.last_activity,
            "vars": {k: v.to_dict() for k, v in self._vars.items()},
            "metadata": self.metadata,
        }


class SessionContextManager:
    def __init__(
        self,
        default_ttl_s: float = 3600.0,
        default_idle_timeout_s: float = 300.0,
        max_sessions: int = 10_000,
    ):
        self.redis: aioredis.Redis | None = None
        self._sessions: dict[str, Session] = {}
        self._user_sessions: dict[str, set[str]] = {}  # user_id -> session_ids
        self._global_ctx: dict[str, Any] = {}
        self._default_ttl = default_ttl_s
        self._default_idle = default_idle_timeout_s
        self._max_sessions = max_sessions
        self._stats: dict[str, int] = {
            "created": 0,
            "closed": 0,
            "expired_purged": 0,
            "get_ops": 0,
            "set_ops": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def create(
        self,
        session_id: str | None = None,
        user_id: str = "",
        ttl_s: float | None = None,
        metadata: dict | None = None,
    ) -> Session:
        import secrets

        sid = session_id or secrets.token_hex(12)

        # Evict if at capacity
        if len(self._sessions) >= self._max_sessions:
            self._evict()

        session = Session(
            session_id=sid,
            user_id=user_id,
            ttl_s=ttl_s or self._default_ttl,
            idle_timeout_s=self._default_idle,
            metadata=metadata or {},
        )
        self._sessions[sid] = session

        if user_id:
            if user_id not in self._user_sessions:
                self._user_sessions[user_id] = set()
            self._user_sessions[user_id].add(sid)

        self._stats["created"] += 1
        log.debug(f"Session created: {sid} user={user_id}")
        return session

    def get(self, session_id: str) -> Session | None:
        session = self._sessions.get(session_id)
        if session and session.is_expired():
            session.state = SessionState.EXPIRED
            return None
        if session and session.is_idle():
            session.state = SessionState.IDLE
        return session

    def get_or_create(self, session_id: str, user_id: str = "") -> Session:
        session = self.get(session_id)
        if session:
            return session
        return self.create(session_id, user_id)

    def close(self, session_id: str):
        session = self._sessions.pop(session_id, None)
        if session:
            session.state = SessionState.CLOSED
            if session.user_id and session.user_id in self._user_sessions:
                self._user_sessions[session.user_id].discard(session_id)
            self._stats["closed"] += 1

    def set(self, session_id: str, key: str, value: Any, **kwargs):
        session = self.get_or_create(session_id)
        session.set(key, value, **kwargs)
        self._stats["set_ops"] += 1

    def get_var(self, session_id: str, key: str, default: Any = None) -> Any:
        session = self.get(session_id)
        if not session:
            # Check global
            return self._global_ctx.get(key, default)
        self._stats["get_ops"] += 1
        val = session.get(key)
        if val is None:
            # Fall through to user-scoped, then global
            val = self._get_user_var(session.user_id, key)
            if val is None:
                val = self._global_ctx.get(key, default)
        return val

    def _get_user_var(self, user_id: str, key: str) -> Any:
        if not user_id:
            return None
        for sid in self._user_sessions.get(user_id, set()):
            s = self._sessions.get(sid)
            if s:
                v = s._vars.get(key)
                if v and v.scope == ContextScope.USER:
                    return v.value
        return None

    def set_global(self, key: str, value: Any):
        self._global_ctx[key] = value

    def get_global(self, key: str, default: Any = None) -> Any:
        return self._global_ctx.get(key, default)

    def user_sessions(self, user_id: str) -> list[Session]:
        sids = self._user_sessions.get(user_id, set())
        return [s for sid in sids if (s := self._sessions.get(sid))]

    def _evict(self):
        # Remove expired first
        expired = [sid for sid, s in self._sessions.items() if s.is_expired()]
        for sid in expired:
            self.close(sid)
            self._stats["expired_purged"] += 1

        # If still over limit, remove oldest idle
        if len(self._sessions) >= self._max_sessions:
            idle = sorted(
                [(sid, s) for sid, s in self._sessions.items() if s.is_idle()],
                key=lambda x: x[1].last_activity,
            )
            for sid, _ in idle[: max(1, len(idle) // 2)]:
                self.close(sid)

    def purge_expired(self) -> int:
        expired = [sid for sid, s in self._sessions.items() if s.is_expired()]
        for sid in expired:
            self.close(sid)
        self._stats["expired_purged"] += len(expired)
        return len(expired)

    async def persist_session(self, session_id: str):
        if not self.redis:
            return
        session = self._sessions.get(session_id)
        if not session:
            return
        try:
            key = f"{REDIS_PREFIX}{session_id}"
            data = json.dumps(session.serialize())
            await self.redis.setex(key, int(session.ttl_s), data)
        except Exception:
            pass

    async def load_session(self, session_id: str) -> Session | None:
        if not self.redis:
            return None
        try:
            key = f"{REDIS_PREFIX}{session_id}"
            data = await self.redis.get(key)
            if not data:
                return None
            d = json.loads(data)
            session = Session(
                session_id=d["session_id"],
                user_id=d.get("user_id", ""),
                state=SessionState(d.get("state", "active")),
                ttl_s=d.get("ttl_s", self._default_ttl),
                idle_timeout_s=d.get("idle_timeout_s", self._default_idle),
                created_at=d.get("created_at", time.time()),
                last_activity=d.get("last_activity", time.time()),
                metadata=d.get("metadata", {}),
            )
            for k, vd in d.get("vars", {}).items():
                session._vars[k] = ContextVar(
                    key=k,
                    value=vd["value"],
                    scope=ContextScope(vd.get("scope", "local")),
                    ttl_s=vd.get("ttl_s", 0.0),
                )
            self._sessions[session_id] = session
            return session
        except Exception:
            return None

    def active_count(self) -> int:
        return sum(1 for s in self._sessions.values() if s.state == SessionState.ACTIVE)

    def stats(self) -> dict:
        return {
            **self._stats,
            "active_sessions": self.active_count(),
            "total_sessions": len(self._sessions),
            "global_vars": len(self._global_ctx),
        }


def build_jarvis_session_context(
    default_ttl_s: float = 3600.0,
) -> SessionContextManager:
    return SessionContextManager(
        default_ttl_s=default_ttl_s,
        default_idle_timeout_s=300.0,
        max_sessions=10_000,
    )


async def main():
    import sys

    mgr = build_jarvis_session_context()
    await mgr.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Session context demo...\n")

        # Create sessions
        s1 = mgr.create("sess_001", user_id="turbo", metadata={"client": "cli"})
        s2 = mgr.create("sess_002", user_id="turbo")

        # Set/get vars
        s1.set("model", "qwen3.5-9b")
        s1.set("lang", "fr")
        s1.set("shared_pref", "dark_mode", scope=ContextScope.USER)

        s2.set("model", "deepseek-r1")
        print(f"  s1 model: {s1.get('model')}")
        print(f"  s2 model: {s2.get('model')}")

        # Global var
        mgr.set_global("cluster_status", "healthy")
        val = mgr.get_var("sess_001", "cluster_status")
        print(f"  global cluster_status via s1: {val}")

        # Snapshot + restore
        s1.set("counter", 10)
        snap = s1.snapshot()
        s1.set("counter", 99)
        print(f"  counter before restore: {s1.get('counter')}")
        s1.restore_snapshot()
        print(f"  counter after restore:  {s1.get('counter')}")

        # Persist + reload
        await mgr.persist_session("sess_001")
        del mgr._sessions["sess_001"]
        reloaded = await mgr.load_session("sess_001")
        if reloaded:
            print(f"  reloaded model: {reloaded.get('model')}")

        print(
            f"\n  User sessions (turbo): {[s.session_id for s in mgr.user_sessions('turbo')]}"
        )
        print(f"\nStats: {json.dumps(mgr.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(mgr.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
