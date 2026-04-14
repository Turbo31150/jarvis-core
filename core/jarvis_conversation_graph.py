#!/usr/bin/env python3
"""
jarvis_conversation_graph — Conversation state as a directed graph
Tracks turns, topics, references, and multi-branch dialogue trees
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.conversation_graph")

CONV_FILE = Path("/home/turbo/IA/Core/jarvis/data/conversations.jsonl")
REDIS_PREFIX = "jarvis:conv:"


class TurnRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class TopicState(str, Enum):
    ACTIVE = "active"
    RESOLVED = "resolved"
    DEFERRED = "deferred"
    ABANDONED = "abandoned"


@dataclass
class ConvTurn:
    turn_id: str
    role: TurnRole
    content: str
    ts: float = field(default_factory=time.time)
    parent_turn_id: str = ""  # which turn this responds to
    topics: list[str] = field(default_factory=list)
    entities: dict[str, str] = field(default_factory=dict)  # entity_type → value
    intent: str = ""
    tokens: int = 0
    model: str = ""
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "turn_id": self.turn_id,
            "role": self.role.value,
            "content": self.content[:500],
            "ts": self.ts,
            "parent_turn_id": self.parent_turn_id,
            "topics": self.topics,
            "entities": self.entities,
            "intent": self.intent,
            "tokens": self.tokens,
            "model": self.model,
        }


@dataclass
class Topic:
    topic_id: str
    label: str
    state: TopicState = TopicState.ACTIVE
    first_turn_id: str = ""
    last_turn_id: str = ""
    mention_count: int = 0
    entities: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "topic_id": self.topic_id,
            "label": self.label,
            "state": self.state.value,
            "mention_count": self.mention_count,
            "entities": self.entities,
        }


@dataclass
class Conversation:
    conv_id: str
    session_id: str = ""
    agent_id: str = ""
    turns: list[ConvTurn] = field(default_factory=list)
    topics: dict[str, Topic] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)

    @property
    def turn_count(self) -> int:
        return len(self.turns)

    @property
    def last_turn(self) -> ConvTurn | None:
        return self.turns[-1] if self.turns else None

    def active_topics(self) -> list[Topic]:
        return [t for t in self.topics.values() if t.state == TopicState.ACTIVE]

    def messages(self, max_turns: int = 0) -> list[dict]:
        """Return OpenAI-style messages list."""
        turns = self.turns[-max_turns:] if max_turns else self.turns
        return [{"role": t.role.value, "content": t.content} for t in turns]

    def to_dict(self, include_turns: bool = True) -> dict:
        d = {
            "conv_id": self.conv_id,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "turn_count": self.turn_count,
            "topics": [t.to_dict() for t in self.topics.values()],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata,
        }
        if include_turns:
            d["turns"] = [t.to_dict() for t in self.turns]
        return d


class ConversationGraph:
    def __init__(self, max_conversations: int = 10_000, persist: bool = True):
        self.redis: aioredis.Redis | None = None
        self._convs: dict[str, Conversation] = {}
        self._max = max_conversations
        self._persist = persist
        self._stats: dict[str, int] = {
            "created": 0,
            "turns_added": 0,
            "topics_tracked": 0,
        }
        if persist:
            CONV_FILE.parent.mkdir(parents=True, exist_ok=True)

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def create(
        self,
        conv_id: str | None = None,
        session_id: str = "",
        agent_id: str = "",
        metadata: dict | None = None,
    ) -> Conversation:
        conv_id = conv_id or str(uuid.uuid4())[:12]
        conv = Conversation(
            conv_id=conv_id,
            session_id=session_id,
            agent_id=agent_id,
            metadata=metadata or {},
        )
        self._convs[conv_id] = conv
        self._stats["created"] += 1

        if len(self._convs) > self._max:
            # Evict oldest
            oldest = min(self._convs, key=lambda k: self._convs[k].created_at)
            del self._convs[oldest]

        return conv

    def get(self, conv_id: str) -> Conversation | None:
        return self._convs.get(conv_id)

    def get_or_create(self, conv_id: str, **kwargs) -> Conversation:
        return self._convs.get(conv_id) or self.create(conv_id=conv_id, **kwargs)

    def add_turn(
        self,
        conv_id: str,
        role: TurnRole,
        content: str,
        parent_turn_id: str = "",
        topics: list[str] | None = None,
        entities: dict | None = None,
        intent: str = "",
        tokens: int = 0,
        model: str = "",
        metadata: dict | None = None,
    ) -> ConvTurn:
        conv = self._convs.get(conv_id)
        if not conv:
            conv = self.create(conv_id=conv_id)

        # Auto-parent to last turn if not specified
        if not parent_turn_id and conv.turns:
            parent_turn_id = conv.turns[-1].turn_id

        turn = ConvTurn(
            turn_id=str(uuid.uuid4())[:10],
            role=role,
            content=content,
            parent_turn_id=parent_turn_id,
            topics=topics or [],
            entities=entities or {},
            intent=intent,
            tokens=tokens,
            model=model,
            metadata=metadata or {},
        )
        conv.turns.append(turn)
        conv.updated_at = time.time()
        self._stats["turns_added"] += 1

        # Update topics
        for label in topics or []:
            self._update_topic(conv, label, turn)

        if self.redis:
            asyncio.create_task(self._redis_save(conv))

        return turn

    def _update_topic(self, conv: Conversation, label: str, turn: ConvTurn):
        topic_id = f"{conv.conv_id}:{label}"
        if topic_id not in conv.topics:
            conv.topics[topic_id] = Topic(
                topic_id=topic_id,
                label=label,
                first_turn_id=turn.turn_id,
            )
            self._stats["topics_tracked"] += 1
        t = conv.topics[topic_id]
        t.last_turn_id = turn.turn_id
        t.mention_count += 1
        t.entities.update(turn.entities)

    def resolve_topic(self, conv_id: str, topic_label: str):
        conv = self._convs.get(conv_id)
        if not conv:
            return
        for t in conv.topics.values():
            if t.label == topic_label:
                t.state = TopicState.RESOLVED

    def branch(self, conv_id: str, from_turn_id: str) -> Conversation:
        """Create a new conversation branching from a specific turn."""
        original = self._convs.get(conv_id)
        if not original:
            raise KeyError(f"Conversation {conv_id!r} not found")

        # Find turn index
        turn_idx = next(
            (i for i, t in enumerate(original.turns) if t.turn_id == from_turn_id),
            len(original.turns),
        )
        new_conv = self.create(
            session_id=original.session_id,
            agent_id=original.agent_id,
            metadata={
                **original.metadata,
                "branched_from": conv_id,
                "branch_turn": from_turn_id,
            },
        )
        # Copy turns up to branch point
        for turn in original.turns[: turn_idx + 1]:
            new_conv.turns.append(turn)

        return new_conv

    def summarize_context(self, conv_id: str, max_turns: int = 10) -> dict:
        conv = self._convs.get(conv_id)
        if not conv:
            return {}
        recent = conv.turns[-max_turns:]
        all_entities: dict[str, str] = {}
        all_intents: list[str] = []
        for t in recent:
            all_entities.update(t.entities)
            if t.intent:
                all_intents.append(t.intent)
        return {
            "conv_id": conv_id,
            "total_turns": conv.turn_count,
            "recent_turns": len(recent),
            "active_topics": [t.label for t in conv.active_topics()],
            "entities": all_entities,
            "recent_intents": list(dict.fromkeys(all_intents))[-5:],
        }

    async def _redis_save(self, conv: Conversation):
        if not self.redis:
            return
        try:
            await self.redis.setex(
                f"{REDIS_PREFIX}{conv.conv_id}",
                3600,
                json.dumps(conv.to_dict(include_turns=False)),
            )
        except Exception:
            pass

    def persist_conv(self, conv_id: str):
        conv = self._convs.get(conv_id)
        if not conv or not self._persist:
            return
        try:
            with open(CONV_FILE, "a") as f:
                f.write(json.dumps(conv.to_dict()) + "\n")
        except Exception:
            pass

    def list_conversations(self, agent_id: str | None = None) -> list[dict]:
        convs = self._convs.values()
        if agent_id:
            convs = [c for c in convs if c.agent_id == agent_id]
        return [c.to_dict(include_turns=False) for c in convs]

    def stats(self) -> dict:
        return {
            **self._stats,
            "active_conversations": len(self._convs),
        }


def build_jarvis_conversation_graph() -> ConversationGraph:
    return ConversationGraph(max_conversations=10_000, persist=True)


async def main():
    import sys

    graph = build_jarvis_conversation_graph()
    await graph.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Conversation graph demo...")
        conv = graph.create(session_id="sess-001", agent_id="inference-gw")
        print(f"  Created conv: {conv.conv_id}")

        graph.add_turn(
            conv.conv_id,
            TurnRole.USER,
            "What GPU nodes are available?",
            topics=["cluster", "gpu"],
            intent="query",
        )
        graph.add_turn(
            conv.conv_id,
            TurnRole.ASSISTANT,
            "M1 (RTX 3060), M2 (RTX 3090), OL1 (RX 6700 XT) are online.",
            topics=["cluster", "gpu"],
            entities={"node": "M1,M2,OL1"},
            model="qwen3.5-9b",
        )
        graph.add_turn(
            conv.conv_id,
            TurnRole.USER,
            "What's the VRAM on M2?",
            topics=["gpu", "vram"],
            intent="query",
            entities={"node": "M2"},
        )

        ctx = graph.summarize_context(conv.conv_id)
        print(f"\n  Context summary: {json.dumps(ctx, indent=4)}")
        print(f"\nStats: {json.dumps(graph.stats(), indent=2)}")

    elif cmd == "list":
        for c in graph.list_conversations():
            print(json.dumps(c))

    elif cmd == "stats":
        print(json.dumps(graph.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
