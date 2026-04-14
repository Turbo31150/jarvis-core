#!/usr/bin/env python3
"""
jarvis_leader_election — Redis-based leader election for cluster coordination
Single-leader election with heartbeat, failover, and observer notifications
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Callable

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.leader_election")

REDIS_PREFIX = "jarvis:election:"
DEFAULT_TTL_S = 15.0
DEFAULT_HEARTBEAT_S = 5.0


@dataclass
class ElectionState:
    group: str
    leader_id: str
    leader_host: str
    elected_at: float
    ttl_s: float
    term: int = 1

    @property
    def remaining_s(self) -> float:
        elapsed = time.time() - self.elected_at
        return max(0.0, self.ttl_s - elapsed)

    def to_dict(self) -> dict:
        return {
            "group": self.group,
            "leader_id": self.leader_id,
            "leader_host": self.leader_host,
            "elected_at": self.elected_at,
            "remaining_s": round(self.remaining_s, 1),
            "term": self.term,
        }


class LeaderElection:
    def __init__(
        self,
        node_id: str | None = None,
        host: str = "localhost",
        ttl_s: float = DEFAULT_TTL_S,
        heartbeat_s: float = DEFAULT_HEARTBEAT_S,
    ):
        self.redis: aioredis.Redis | None = None
        self.node_id = node_id or str(uuid.uuid4())[:10]
        self.host = host
        self.ttl_s = ttl_s
        self.heartbeat_s = heartbeat_s
        self._is_leader: dict[str, bool] = {}
        self._term: dict[str, int] = {}
        self._heartbeat_tasks: dict[str, asyncio.Task] = {}
        self._on_elected: list[Callable[[str], None]] = []
        self._on_demoted: list[Callable[[str], None]] = []
        self._stats: dict[str, int] = {
            "elections_won": 0,
            "elections_lost": 0,
            "heartbeats_sent": 0,
            "failovers_detected": 0,
        }

    async def connect_redis(self):
        self.redis = aioredis.Redis(decode_responses=True)
        await self.redis.ping()

    def on_elected(self, callback: Callable[[str], None]):
        """Called with group name when this node becomes leader."""
        self._on_elected.append(callback)

    def on_demoted(self, callback: Callable[[str], None]):
        """Called with group name when this node loses leadership."""
        self._on_demoted.append(callback)

    def _election_key(self, group: str) -> str:
        return f"{REDIS_PREFIX}{group}:leader"

    def _term_key(self, group: str) -> str:
        return f"{REDIS_PREFIX}{group}:term"

    def _value(self) -> str:
        return json.dumps(
            {"node_id": self.node_id, "host": self.host, "ts": time.time()}
        )

    async def try_become_leader(self, group: str) -> bool:
        """Attempt to acquire leadership. Returns True if successful."""
        if not self.redis:
            return False
        key = self._election_key(group)
        value = self._value()
        ttl_ms = int(self.ttl_s * 1000)

        result = await self.redis.set(key, value, nx=True, px=ttl_ms)
        if result:
            term = await self.redis.incr(self._term_key(group))
            self._is_leader[group] = True
            self._term[group] = int(term)
            self._stats["elections_won"] += 1
            log.info(f"[{group}] Elected leader: {self.node_id} (term={term})")
            for cb in self._on_elected:
                try:
                    cb(group)
                except Exception:
                    pass
            return True
        else:
            self._is_leader[group] = False
            self._stats["elections_lost"] += 1
            return False

    async def heartbeat(self, group: str) -> bool:
        """Renew leadership TTL. Returns False if leadership was lost."""
        if not self.redis or not self._is_leader.get(group):
            return False
        key = self._election_key(group)
        # Check we still own it
        raw = await self.redis.get(key)
        if not raw:
            await self._handle_demotion(group)
            return False
        try:
            data = json.loads(raw)
            if data.get("node_id") != self.node_id:
                await self._handle_demotion(group)
                return False
        except Exception:
            pass

        # Re-set with new TTL (update timestamp)
        value = self._value()
        ttl_ms = int(self.ttl_s * 1000)
        await self.redis.set(key, value, px=ttl_ms, xx=True)
        self._stats["heartbeats_sent"] += 1
        return True

    async def _handle_demotion(self, group: str):
        was_leader = self._is_leader.get(group, False)
        self._is_leader[group] = False
        if was_leader:
            log.warning(f"[{group}] Lost leadership: {self.node_id}")
            self._stats["failovers_detected"] += 1
            for cb in self._on_demoted:
                try:
                    cb(group)
                except Exception:
                    pass

    async def step_down(self, group: str) -> bool:
        """Voluntarily release leadership."""
        if not self.redis or not self._is_leader.get(group):
            return False
        key = self._election_key(group)
        raw = await self.redis.get(key)
        if raw:
            try:
                data = json.loads(raw)
                if data.get("node_id") == self.node_id:
                    await self.redis.delete(key)
            except Exception:
                pass
        await self._handle_demotion(group)
        # Stop heartbeat
        task = self._heartbeat_tasks.pop(group, None)
        if task:
            task.cancel()
        return True

    async def get_leader(self, group: str) -> ElectionState | None:
        if not self.redis:
            return None
        key = self._election_key(group)
        raw = await self.redis.get(key)
        if not raw:
            return None
        try:
            data = json.loads(raw)
            ttl_ms = await self.redis.pttl(key)
            term_val = await self.redis.get(self._term_key(group))
            return ElectionState(
                group=group,
                leader_id=data.get("node_id", ""),
                leader_host=data.get("host", ""),
                elected_at=data.get("ts", time.time()),
                ttl_s=max(0, ttl_ms / 1000),
                term=int(term_val or 0),
            )
        except Exception:
            return None

    async def is_leader(self, group: str) -> bool:
        return self._is_leader.get(group, False)

    async def run(self, group: str):
        """Main election loop: keep trying to become/remain leader."""
        while True:
            try:
                if not self._is_leader.get(group):
                    await self.try_become_leader(group)
                else:
                    ok = await self.heartbeat(group)
                    if not ok:
                        await self.try_become_leader(group)
            except Exception as e:
                log.error(f"Election loop error [{group}]: {e}")
            await asyncio.sleep(self.heartbeat_s)

    def start(self, group: str) -> asyncio.Task:
        task = asyncio.create_task(self.run(group))
        self._heartbeat_tasks[group] = task
        return task

    async def stop(self, group: str):
        await self.step_down(group)

    def stats(self) -> dict:
        return {
            **self._stats,
            "node_id": self.node_id,
            "leading": [g for g, v in self._is_leader.items() if v],
        }


async def main():
    import sys

    node = LeaderElection(node_id="m1-primary", host="192.168.1.85", ttl_s=10.0)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    try:
        await node.connect_redis()
    except Exception as e:
        print(f"Redis unavailable: {e}")
        return

    if cmd == "demo":
        elected_events = []
        node.on_elected(lambda g: elected_events.append(f"elected:{g}"))
        node.on_demoted(lambda g: elected_events.append(f"demoted:{g}"))

        # First election
        won = await node.try_become_leader("inference-coordinator")
        print(f"Election 1: {'won' if won else 'lost'}")

        # Heartbeat
        ok = await node.heartbeat("inference-coordinator")
        print(f"Heartbeat: {'ok' if ok else 'failed'}")

        # Get leader info
        state = await node.get_leader("inference-coordinator")
        if state:
            print(
                f"Leader: {state.leader_id} (term={state.term}, ttl={state.remaining_s:.1f}s)"
            )

        # Second node tries to win (should fail — already locked)
        node2 = LeaderElection(node_id="m2-backup", host="192.168.1.26", ttl_s=10.0)
        await node2.connect_redis()
        won2 = await node2.try_become_leader("inference-coordinator")
        print(f"Node2 election: {'won' if won2 else 'lost (expected)'}")

        # Step down
        await node.step_down("inference-coordinator")
        print(f"Stepped down. Events: {elected_events}")

        # Now node2 can win
        won2_retry = await node2.try_become_leader("inference-coordinator")
        print(f"Node2 retry: {'won' if won2_retry else 'lost'}")
        if won2_retry:
            await node2.step_down("inference-coordinator")

        print(f"\nStats: {json.dumps(node.stats(), indent=2)}")

    elif cmd == "status":
        state = await node.get_leader(sys.argv[2] if len(sys.argv) > 2 else "default")
        if state:
            print(json.dumps(state.to_dict(), indent=2))
        else:
            print("No leader")

    elif cmd == "stats":
        print(json.dumps(node.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
