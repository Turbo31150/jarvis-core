#!/usr/bin/env python3
"""
jarvis_replay_buffer — Experience replay buffer for RL/adaptive systems
Prioritized sampling, sliding window, episode tracking, Redis persistence
"""

import asyncio
import json
import logging
import math
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.replay_buffer")

BUFFER_FILE = Path("/home/turbo/IA/Core/jarvis/data/replay_buffer.jsonl")
REDIS_PREFIX = "jarvis:replay:"


class SamplingStrategy(str, Enum):
    UNIFORM = "uniform"
    PRIORITIZED = "prioritized"  # proportional to priority
    RECENCY = "recency"  # prefer recent experiences
    EPISODE = "episode"  # sample full episodes


@dataclass
class Experience:
    exp_id: str
    state: Any
    action: Any
    reward: float
    next_state: Any
    done: bool
    episode_id: str = ""
    step: int = 0
    priority: float = 1.0
    ts: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "exp_id": self.exp_id,
            "state": self.state,
            "action": self.action,
            "reward": round(self.reward, 4),
            "next_state": self.next_state,
            "done": self.done,
            "episode_id": self.episode_id,
            "step": self.step,
            "priority": round(self.priority, 4),
            "ts": self.ts,
            "metadata": self.metadata,
        }


@dataclass
class Episode:
    episode_id: str
    experiences: list[Experience] = field(default_factory=list)
    total_reward: float = 0.0
    steps: int = 0
    done: bool = False
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "episode_id": self.episode_id,
            "steps": self.steps,
            "total_reward": round(self.total_reward, 4),
            "done": self.done,
            "ts": self.ts,
        }


class ReplayBuffer:
    def __init__(
        self,
        capacity: int = 10_000,
        strategy: SamplingStrategy = SamplingStrategy.PRIORITIZED,
        alpha: float = 0.6,  # priority exponent
        beta: float = 0.4,  # IS correction exponent
        persist: bool = True,
    ):
        self.redis: aioredis.Redis | None = None
        self._capacity = capacity
        self._strategy = strategy
        self._alpha = alpha
        self._beta = beta
        self._persist = persist
        self._buffer: list[Experience] = []
        self._episodes: dict[str, Episode] = {}
        self._write_idx = 0
        self._total_added = 0
        self._priority_sum = 0.0
        self._stats: dict[str, int] = {
            "added": 0,
            "sampled": 0,
            "evictions": 0,
            "episodes_completed": 0,
        }
        if persist:
            BUFFER_FILE.parent.mkdir(parents=True, exist_ok=True)
            self._load()

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def _load(self):
        if not BUFFER_FILE.exists():
            return
        try:
            loaded = 0
            for line in BUFFER_FILE.read_text().splitlines():
                if not line.strip():
                    continue
                d = json.loads(line)
                exp = Experience(
                    exp_id=d["exp_id"],
                    state=d["state"],
                    action=d["action"],
                    reward=d["reward"],
                    next_state=d["next_state"],
                    done=d["done"],
                    episode_id=d.get("episode_id", ""),
                    step=d.get("step", 0),
                    priority=d.get("priority", 1.0),
                    ts=d.get("ts", time.time()),
                    metadata=d.get("metadata", {}),
                )
                self._buffer.append(exp)
                self._priority_sum += exp.priority**self._alpha
                loaded += 1
                if loaded >= self._capacity:
                    break
            log.info(f"Replay buffer loaded {loaded} experiences")
        except Exception as e:
            log.warning(f"Replay buffer load error: {e}")

    def add(self, exp: Experience, max_priority: float | None = None) -> str:
        if max_priority is None:
            max_priority = max((e.priority for e in self._buffer), default=1.0)
        exp.priority = max_priority  # new experiences get max priority

        if len(self._buffer) < self._capacity:
            self._buffer.append(exp)
        else:
            # Overwrite oldest (circular)
            old = self._buffer[self._write_idx]
            self._priority_sum -= old.priority**self._alpha
            self._buffer[self._write_idx] = exp
            self._write_idx = (self._write_idx + 1) % self._capacity
            self._stats["evictions"] += 1

        self._priority_sum += exp.priority**self._alpha
        self._total_added += 1
        self._stats["added"] += 1

        # Track episode
        if exp.episode_id:
            ep = self._episodes.setdefault(
                exp.episode_id, Episode(episode_id=exp.episode_id)
            )
            ep.experiences.append(exp)
            ep.total_reward += exp.reward
            ep.steps += 1
            if exp.done:
                ep.done = True
                self._stats["episodes_completed"] += 1

        if self._persist and self._total_added % 100 == 0:
            asyncio.create_task(self._flush_recent(50))

        return exp.exp_id

    def update_priorities(self, exp_ids: list[str], priorities: list[float]):
        id_set = dict(zip(exp_ids, priorities))
        for exp in self._buffer:
            if exp.exp_id in id_set:
                old_p = exp.priority**self._alpha
                exp.priority = max(1e-6, id_set[exp.exp_id])
                new_p = exp.priority**self._alpha
                self._priority_sum += new_p - old_p

    def sample(
        self, batch_size: int
    ) -> tuple[list[Experience], list[float], list[str]]:
        """Returns (experiences, IS weights, exp_ids)."""
        n = len(self._buffer)
        if n == 0:
            return [], [], []
        batch_size = min(batch_size, n)
        self._stats["sampled"] += batch_size

        if self._strategy == SamplingStrategy.UNIFORM:
            sampled = random.sample(self._buffer, batch_size)
            weights = [1.0] * batch_size

        elif self._strategy == SamplingStrategy.PRIORITIZED:
            probs = [
                (e.priority**self._alpha) / max(self._priority_sum, 1e-9)
                for e in self._buffer
            ]
            indices = random.choices(range(n), weights=probs, k=batch_size)
            sampled = [self._buffer[i] for i in indices]
            min_prob = min(probs)
            weights = [(n * probs[i]) ** (-self._beta) for i in indices]
            max_w = max(weights)
            weights = [w / max_w for w in weights]

        elif self._strategy == SamplingStrategy.RECENCY:
            # Prefer recent: weight by recency rank
            sorted_buf = sorted(self._buffer, key=lambda e: -e.ts)
            weights_r = [math.exp(-i / max(n, 1)) for i in range(n)]
            indices = random.choices(range(n), weights=weights_r, k=batch_size)
            sampled = [sorted_buf[i] for i in indices]
            weights = [1.0] * batch_size

        else:
            sampled = random.sample(self._buffer, batch_size)
            weights = [1.0] * batch_size

        return sampled, weights, [e.exp_id for e in sampled]

    def sample_episode(self, episode_id: str) -> list[Experience]:
        ep = self._episodes.get(episode_id)
        return ep.experiences if ep else []

    async def _flush_recent(self, n: int):
        try:
            recent = self._buffer[-n:]
            with open(BUFFER_FILE, "a") as f:
                for exp in recent:
                    f.write(json.dumps(exp.to_dict()) + "\n")
        except Exception:
            pass

    def __len__(self) -> int:
        return len(self._buffer)

    def episodes(self) -> list[dict]:
        return [ep.to_dict() for ep in self._episodes.values()]

    def stats(self) -> dict:
        n = len(self._buffer)
        avg_reward = sum(e.reward for e in self._buffer) / max(n, 1)
        return {
            **self._stats,
            "size": n,
            "capacity": self._capacity,
            "fill_pct": round(n / self._capacity * 100, 1),
            "avg_reward": round(avg_reward, 4),
            "strategy": self._strategy.value,
            "active_episodes": len(self._episodes),
        }


def build_jarvis_replay_buffer(capacity: int = 50_000) -> ReplayBuffer:
    return ReplayBuffer(
        capacity=capacity,
        strategy=SamplingStrategy.PRIORITIZED,
        alpha=0.6,
        beta=0.4,
        persist=True,
    )


async def main():
    import sys
    import uuid

    buf = build_jarvis_replay_buffer(capacity=1000)
    await buf.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        ep_id = str(uuid.uuid4())[:8]
        print(f"Adding 20 experiences (episode {ep_id})...")
        for i in range(20):
            exp = Experience(
                exp_id=str(uuid.uuid4())[:8],
                state={"step": i, "val": float(i) / 20},
                action=random.choice(["left", "right", "up", "down"]),
                reward=random.gauss(0, 1),
                next_state={"step": i + 1, "val": float(i + 1) / 20},
                done=(i == 19),
                episode_id=ep_id,
                step=i,
                priority=abs(random.gauss(1, 0.5)),
            )
            buf.add(exp)

        print(f"Buffer size: {len(buf)}")

        batch, weights, ids = buf.sample(5)
        print("\nSample batch (prioritized, size=5):")
        for exp, w in zip(batch, weights):
            print(
                f"  {exp.exp_id} step={exp.step:<3} reward={exp.reward:+.3f} "
                f"priority={exp.priority:.3f} IS_weight={w:.3f}"
            )

        print(f"\nStats: {json.dumps(buf.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(buf.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
