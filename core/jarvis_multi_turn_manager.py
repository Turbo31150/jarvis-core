#!/usr/bin/env python3
"""
jarvis_multi_turn_manager — Multi-turn conversation lifecycle management
Turn tracking, context window management, auto-compression, session switching.
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

log = logging.getLogger("jarvis.multi_turn_manager")


class TurnRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class CompressionPolicy(str, Enum):
    NONE = "none"
    SUMMARIZE_OLD = "summarize_old"   # summarize turns beyond window
    DROP_MIDDLE = "drop_middle"       # keep first + last N turns
    TRIM_CONTENT = "trim_content"     # truncate long turn content


@dataclass
class Turn:
    turn_id: int
    role: TurnRole
    content: str
    ts: float = field(default_factory=time.time)
    tokens: int = 0           # estimated token count
    compressed: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def estimate_tokens(self) -> int:
        """Rough: 1 token ≈ 4 chars."""
        self.tokens = max(1, len(self.content) // 4)
        return self.tokens

    def to_openai(self) -> dict:
        return {"role": self.role.value, "content": self.content}

    def to_dict(self) -> dict:
        return {
            "turn_id": self.turn_id,
            "role": self.role.value,
            "content": self.content[:100],
            "tokens": self.tokens,
            "compressed": self.compressed,
            "age_s": round(time.time() - self.ts, 1),
        }


@dataclass
class ConversationSession:
    session_id: str
    turns: list[Turn] = field(default_factory=list)
    system_prompt: str = ""
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)
    _turn_counter: int = 0

    def add_turn(self, role: TurnRole, content: str, **meta) -> Turn:
        self._turn_counter += 1
        t = Turn(
            turn_id=self._turn_counter,
            role=role,
            content=content,
            metadata=meta,
        )
        t.estimate_tokens()
        self.turns.append(t)
        self.last_activity = time.time()
        return t

    def total_tokens(self) -> int:
        base = len(self.system_prompt) // 4
        return base + sum(t.tokens for t in self.turns)

    def user_turns(self) -> list[Turn]:
        return [t for t in self.turns if t.role == TurnRole.USER]

    def last_user_turn(self) -> Turn | None:
        for t in reversed(self.turns):
            if t.role == TurnRole.USER:
                return t
        return None

    def last_assistant_turn(self) -> Turn | None:
        for t in reversed(self.turns):
            if t.role == TurnRole.ASSISTANT:
                return t
        return None

    def to_openai_messages(self) -> list[dict]:
        msgs = []
        if self.system_prompt:
            msgs.append({"role": "system", "content": self.system_prompt})
        msgs.extend(t.to_openai() for t in self.turns)
        return msgs

    def is_stale(self, ttl_s: float = 3600.0) -> bool:
        return (time.time() - self.last_activity) > ttl_s

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "turn_count": len(self.turns),
            "total_tokens": self.total_tokens(),
            "age_s": round(time.time() - self.created_at),
            "last_activity_s": round(time.time() - self.last_activity),
        }


class MultiTurnManager:
    """
    Manages multiple concurrent conversation sessions.
    Handles context window limits with configurable compression policies.
    """

    def __init__(
        self,
        max_tokens: int = 8192,
        compression_policy: CompressionPolicy = CompressionPolicy.DROP_MIDDLE,
        keep_first_n: int = 2,
        keep_last_n: int = 10,
        session_ttl_s: float = 3600.0,
    ):
        self._sessions: dict[str, ConversationSession] = {}
        self._max_tokens = max_tokens
        self._policy = compression_policy
        self._keep_first = keep_first_n
        self._keep_last = keep_last_n
        self._session_ttl = session_ttl_s
        self._stats: dict[str, int] = {
            "sessions_created": 0,
            "turns_added": 0,
            "compressions": 0,
            "sessions_expired": 0,
        }

    def _new_session_id(self) -> str:
        import secrets
        return secrets.token_hex(8)

    def create_session(
        self,
        session_id: str | None = None,
        system_prompt: str = "",
        **meta,
    ) -> ConversationSession:
        sid = session_id or self._new_session_id()
        session = ConversationSession(
            session_id=sid,
            system_prompt=system_prompt,
            metadata=meta,
        )
        self._sessions[sid] = session
        self._stats["sessions_created"] += 1
        return session

    def get_session(self, session_id: str) -> ConversationSession | None:
        return self._sessions.get(session_id)

    def get_or_create(self, session_id: str, system_prompt: str = "") -> ConversationSession:
        if session_id not in self._sessions:
            return self.create_session(session_id, system_prompt)
        return self._sessions[session_id]

    def add_turn(
        self,
        session_id: str,
        role: TurnRole | str,
        content: str,
        auto_compress: bool = True,
    ) -> Turn:
        session = self.get_or_create(session_id)
        if isinstance(role, str):
            role = TurnRole(role)
        turn = session.add_turn(role, content)
        self._stats["turns_added"] += 1

        if auto_compress and session.total_tokens() > self._max_tokens:
            self._compress(session)

        return turn

    def _compress(self, session: ConversationSession):
        if self._policy == CompressionPolicy.NONE:
            return
        if self._policy == CompressionPolicy.DROP_MIDDLE:
            self._drop_middle(session)
        elif self._policy == CompressionPolicy.TRIM_CONTENT:
            self._trim_content(session)
        elif self._policy == CompressionPolicy.SUMMARIZE_OLD:
            self._summarize_old(session)
        self._stats["compressions"] += 1
        log.debug(f"Compressed session {session.session_id}: {session.total_tokens()} tokens")

    def _drop_middle(self, session: ConversationSession):
        turns = session.turns
        if len(turns) <= self._keep_first + self._keep_last:
            return
        keep_first = turns[:self._keep_first]
        keep_last = turns[-self._keep_last:]
        dropped = turns[self._keep_first:-self._keep_last]
        # Insert a placeholder
        placeholder = Turn(
            turn_id=0,
            role=TurnRole.SYSTEM,
            content=f"[{len(dropped)} turns omitted for context window]",
            compressed=True,
        )
        placeholder.estimate_tokens()
        session.turns = keep_first + [placeholder] + keep_last

    def _trim_content(self, session: ConversationSession):
        max_per_turn = (self._max_tokens * 4) // max(len(session.turns), 1)
        for t in session.turns:
            if len(t.content) > max_per_turn:
                t.content = t.content[:max_per_turn] + " [truncated]"
                t.estimate_tokens()
                t.compressed = True

    def _summarize_old(self, session: ConversationSession):
        """Simple keyword-based summarization for old turns."""
        cutoff = max(0, len(session.turns) - self._keep_last)
        old_turns = session.turns[:cutoff]
        if not old_turns:
            return
        combined = " ".join(t.content for t in old_turns if t.role != TurnRole.SYSTEM)
        # Extract key sentences (first sentence of each turn)
        summary_parts = []
        for t in old_turns:
            if t.role in (TurnRole.USER, TurnRole.ASSISTANT):
                first_sent = re.split(r"[.!?]", t.content)[0].strip()[:60]
                if first_sent:
                    summary_parts.append(f"{t.role.value}: {first_sent}")
        summary = "Summary of earlier conversation: " + "; ".join(summary_parts[:5])
        placeholder = Turn(
            turn_id=0,
            role=TurnRole.SYSTEM,
            content=summary,
            compressed=True,
        )
        placeholder.estimate_tokens()
        session.turns = [placeholder] + session.turns[cutoff:]

    def get_context(self, session_id: str) -> list[dict]:
        """Return OpenAI-format messages for the session."""
        session = self._sessions.get(session_id)
        if not session:
            return []
        return session.to_openai_messages()

    def expire_stale(self) -> int:
        expired = [
            sid for sid, s in self._sessions.items()
            if s.is_stale(self._session_ttl)
        ]
        for sid in expired:
            del self._sessions[sid]
            self._stats["sessions_expired"] += 1
        return len(expired)

    def list_sessions(self) -> list[dict]:
        return [s.to_dict() for s in self._sessions.values()]

    def stats(self) -> dict:
        return {
            **self._stats,
            "active_sessions": len(self._sessions),
            "total_turns": sum(len(s.turns) for s in self._sessions.values()),
            "total_tokens": sum(s.total_tokens() for s in self._sessions.values()),
        }


def build_jarvis_multi_turn_manager(
    max_tokens: int = 8192,
    policy: str = "drop_middle",
) -> MultiTurnManager:
    return MultiTurnManager(
        max_tokens=max_tokens,
        compression_policy=CompressionPolicy(policy),
    )


async def main():
    import sys
    mgr = build_jarvis_multi_turn_manager(max_tokens=2000)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Multi-turn manager demo...\n")

        s = mgr.create_session("sess-1", system_prompt="You are a helpful assistant.")
        mgr.add_turn("sess-1", TurnRole.USER, "Hello, what is the weather today?")
        mgr.add_turn("sess-1", TurnRole.ASSISTANT, "I don't have real-time data, but I can help with forecasts.")
        mgr.add_turn("sess-1", TurnRole.USER, "Can you analyze GPU performance metrics?")
        mgr.add_turn("sess-1", TurnRole.ASSISTANT, "Sure, I can analyze GPU performance. Please provide the data.")

        # Add many turns to trigger compression
        for i in range(20):
            mgr.add_turn("sess-1", TurnRole.USER, f"Question {i}: " + "x" * 100)
            mgr.add_turn("sess-1", TurnRole.ASSISTANT, f"Answer {i}: " + "y" * 80)

        ctx = mgr.get_context("sess-1")
        print(f"  Session tokens: {s.total_tokens()}")
        print(f"  Context messages: {len(ctx)}")
        print(f"  Compressions: {mgr._stats['compressions']}")
        for msg in ctx[:5]:
            print(f"    [{msg['role']:10}] {msg['content'][:60]}")

        print(f"\nStats: {json.dumps(mgr.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(mgr.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
