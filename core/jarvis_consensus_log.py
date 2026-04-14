#!/usr/bin/env python3
"""
jarvis_consensus_log — Append-only consensus log for multi-agent decisions
Raft-inspired log entries, quorum tracking, commit index management
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.consensus_log")

REDIS_PREFIX = "jarvis:clog:"
LOG_STREAM = "jarvis:clog:entries"


class EntryState(str, Enum):
    PROPOSED = "proposed"
    ACKNOWLEDGED = "acknowledged"
    COMMITTED = "committed"
    ABORTED = "aborted"


class VoteResult(str, Enum):
    GRANTED = "granted"
    DENIED = "denied"
    TIMEOUT = "timeout"


@dataclass
class LogEntry:
    index: int
    term: int
    command: str
    payload: dict[str, Any]
    proposer: str
    state: EntryState = EntryState.PROPOSED
    votes: dict[str, bool] = field(default_factory=dict)  # node_id → vote
    created_at: float = field(default_factory=time.time)
    committed_at: float = 0.0
    quorum_size: int = 2

    @property
    def vote_count(self) -> int:
        return sum(1 for v in self.votes.values() if v)

    @property
    def has_quorum(self) -> bool:
        return self.vote_count >= self.quorum_size

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "term": self.term,
            "command": self.command,
            "payload": self.payload,
            "proposer": self.proposer,
            "state": self.state.value,
            "votes": self.votes,
            "vote_count": self.vote_count,
            "quorum_size": self.quorum_size,
            "has_quorum": self.has_quorum,
            "created_at": self.created_at,
            "committed_at": self.committed_at,
        }


@dataclass
class ConsensusResult:
    accepted: bool
    index: int
    entry: LogEntry
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "index": self.index,
            "reason": self.reason,
        }


class ConsensusLog:
    def __init__(self, node_id: str, quorum_size: int = 2):
        self.redis: aioredis.Redis | None = None
        self._node_id = node_id
        self._quorum_size = quorum_size
        self._entries: list[LogEntry] = []
        self._commit_index: int = -1
        self._current_term: int = 0
        self._pending: dict[int, asyncio.Event] = {}  # index → event
        self._apply_callbacks: list = []
        self._stats: dict[str, int] = {
            "proposed": 0,
            "committed": 0,
            "aborted": 0,
            "votes_cast": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def on_commit(self, callback):
        self._apply_callbacks.append(callback)

    def _notify_apply(self, entry: LogEntry):
        for cb in self._apply_callbacks:
            try:
                cb(entry)
            except Exception:
                pass

    def _next_index(self) -> int:
        return len(self._entries)

    def propose(
        self,
        command: str,
        payload: dict,
        auto_vote: bool = True,
    ) -> LogEntry:
        idx = self._next_index()
        entry = LogEntry(
            index=idx,
            term=self._current_term,
            command=command,
            payload=payload,
            proposer=self._node_id,
            quorum_size=self._quorum_size,
        )
        self._entries.append(entry)
        self._pending[idx] = asyncio.Event()
        self._stats["proposed"] += 1

        # Auto-vote from self
        if auto_vote:
            self.vote(idx, self._node_id, True)

        log.debug(f"Proposed entry [{idx}] cmd={command!r} term={self._current_term}")

        if self.redis:
            asyncio.create_task(self._redis_store(entry))

        return entry

    def vote(self, index: int, voter: str, granted: bool) -> bool:
        if index >= len(self._entries):
            return False
        entry = self._entries[index]
        if entry.state in (EntryState.COMMITTED, EntryState.ABORTED):
            return False

        entry.votes[voter] = granted
        self._stats["votes_cast"] += 1

        if entry.state == EntryState.PROPOSED and len(entry.votes) >= 1:
            entry.state = EntryState.ACKNOWLEDGED

        # Check for quorum
        if entry.has_quorum and entry.state != EntryState.COMMITTED:
            self._commit(entry)

        # Check for denial quorum (majority denies)
        denial_count = sum(1 for v in entry.votes.values() if not v)
        if denial_count >= self._quorum_size:
            self._abort(entry, "denial quorum reached")

        return True

    def _commit(self, entry: LogEntry):
        entry.state = EntryState.COMMITTED
        entry.committed_at = time.time()
        self._commit_index = max(self._commit_index, entry.index)
        self._stats["committed"] += 1
        self._notify_apply(entry)

        if entry.index in self._pending:
            self._pending[entry.index].set()

        log.info(
            f"Entry [{entry.index}] committed cmd={entry.command!r} votes={entry.vote_count}/{entry.quorum_size}"
        )

        if self.redis:
            asyncio.create_task(self._redis_store(entry))
            asyncio.create_task(self._redis_publish(entry))

    def _abort(self, entry: LogEntry, reason: str = ""):
        entry.state = EntryState.ABORTED
        self._stats["aborted"] += 1

        if entry.index in self._pending:
            self._pending[entry.index].set()

        log.warning(f"Entry [{entry.index}] aborted: {reason}")

    async def wait_for_commit(
        self, index: int, timeout_s: float = 10.0
    ) -> ConsensusResult:
        if index >= len(self._entries):
            return ConsensusResult(
                False, index, LogEntry(index, 0, "", {}, ""), "index out of range"
            )

        event = self._pending.get(index)
        if not event:
            event = asyncio.Event()
            self._pending[index] = event

        entry = self._entries[index]
        if entry.state == EntryState.COMMITTED:
            return ConsensusResult(True, index, entry, "already committed")

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            return ConsensusResult(False, index, entry, f"timeout after {timeout_s}s")

        entry = self._entries[index]
        return ConsensusResult(
            entry.state == EntryState.COMMITTED,
            index,
            entry,
            entry.state.value,
        )

    async def propose_and_wait(
        self,
        command: str,
        payload: dict,
        timeout_s: float = 10.0,
    ) -> ConsensusResult:
        entry = self.propose(command, payload)
        return await self.wait_for_commit(entry.index, timeout_s)

    async def _redis_store(self, entry: LogEntry):
        if not self.redis:
            return
        try:
            await self.redis.setex(
                f"{REDIS_PREFIX}{entry.index}",
                3600,
                json.dumps(entry.to_dict()),
            )
        except Exception:
            pass

    async def _redis_publish(self, entry: LogEntry):
        if not self.redis:
            return
        try:
            await self.redis.xadd(
                LOG_STREAM,
                {"data": json.dumps(entry.to_dict())},
                maxlen=10000,
            )
        except Exception:
            pass

    def get_entry(self, index: int) -> LogEntry | None:
        if 0 <= index < len(self._entries):
            return self._entries[index]
        return None

    def committed_entries(self) -> list[LogEntry]:
        return [e for e in self._entries if e.state == EntryState.COMMITTED]

    def pending_entries(self) -> list[LogEntry]:
        return [
            e
            for e in self._entries
            if e.state in (EntryState.PROPOSED, EntryState.ACKNOWLEDGED)
        ]

    def log_tail(self, limit: int = 20) -> list[dict]:
        return [e.to_dict() for e in self._entries[-limit:]]

    def stats(self) -> dict:
        by_state: dict[str, int] = {}
        for e in self._entries:
            by_state[e.state.value] = by_state.get(e.state.value, 0) + 1
        return {
            **self._stats,
            "total_entries": len(self._entries),
            "commit_index": self._commit_index,
            "current_term": self._current_term,
            "by_state": by_state,
        }


def build_jarvis_consensus_log(
    node_id: str = "m1", quorum_size: int = 2
) -> ConsensusLog:
    clog = ConsensusLog(node_id=node_id, quorum_size=quorum_size)

    def on_commit(entry: LogEntry):
        log.info(f"[apply] cmd={entry.command!r} index={entry.index}")

    clog.on_commit(on_commit)
    return clog


async def main():
    import sys

    clog = build_jarvis_consensus_log(node_id="m1", quorum_size=2)
    await clog.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Consensus log demo (quorum=2)...")

        # Propose with auto-vote from m1, then m2 votes yes → commit
        e1 = clog.propose("model.load", {"model": "qwen3.5-9b", "node": "m1"})
        clog.vote(e1.index, "m2", True)  # quorum reached

        # Propose, m2 votes no → abort
        e2 = clog.propose("trading.enable", {"symbol": "BTC"})
        clog.vote(e2.index, "m2", False)

        # propose_and_wait with simulated delayed vote
        async def delayed_vote():
            await asyncio.sleep(0.05)
            clog.vote(2, "m2", True)

        asyncio.create_task(delayed_vote())
        r = await clog.propose_and_wait(
            "config.set", {"key": "log_level", "value": "debug"}, timeout_s=2.0
        )

        print(f"\n  Entry 0 (model.load)   → {clog.get_entry(0).state.value}")
        print(f"  Entry 1 (trading)      → {clog.get_entry(1).state.value}")
        print(f"  Entry 2 (config.set)   → {r.entry.state.value} accepted={r.accepted}")

        print(f"\n  Committed entries: {len(clog.committed_entries())}")
        print(f"  Pending entries:   {len(clog.pending_entries())}")

        print(f"\nStats: {json.dumps(clog.stats(), indent=2)}")

    elif cmd == "log":
        for e in clog.log_tail():
            print(
                f"  [{e['index']:3d}] {e['command']:<25} {e['state']:<15} votes={e['vote_count']}/{e['quorum_size']}"
            )

    elif cmd == "stats":
        print(json.dumps(clog.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
