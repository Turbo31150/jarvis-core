#!/usr/bin/env python3
"""
jarvis_agent_registry — Central registry for JARVIS agents with capability discovery
Tracks active agents, their capabilities, health, and routes tasks to best-fit agents
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.agent_registry")

REDIS_PREFIX = "jarvis:agents:"
REDIS_CHANNEL = "jarvis:agent_events"


class AgentStatus(str, Enum):
    IDLE = "idle"
    BUSY = "busy"
    DEGRADED = "degraded"
    OFFLINE = "offline"
    STARTING = "starting"


class AgentCapability(str, Enum):
    INFERENCE = "inference"
    EMBEDDING = "embedding"
    CODE_GEN = "code_gen"
    REASONING = "reasoning"
    SEARCH = "search"
    TRADING = "trading"
    MONITORING = "monitoring"
    BROWSER = "browser"
    VOICE = "voice"
    ORCHESTRATION = "orchestration"


@dataclass
class AgentDescriptor:
    agent_id: str
    name: str
    node: str  # "m1", "m2", "ol1", "local"
    capabilities: list[AgentCapability] = field(default_factory=list)
    status: AgentStatus = AgentStatus.IDLE
    max_concurrency: int = 1
    current_tasks: int = 0
    priority: int = 5  # higher = preferred
    tags: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    registered_at: float = field(default_factory=time.time)
    last_heartbeat: float = field(default_factory=time.time)
    total_tasks: int = 0
    failed_tasks: int = 0

    @property
    def available(self) -> bool:
        return (
            self.status in (AgentStatus.IDLE, AgentStatus.BUSY)
            and self.current_tasks < self.max_concurrency
        )

    @property
    def load_pct(self) -> float:
        return (self.current_tasks / max(self.max_concurrency, 1)) * 100

    @property
    def failure_rate(self) -> float:
        return self.failed_tasks / max(self.total_tasks, 1)

    @property
    def is_stale(self) -> bool:
        return (time.time() - self.last_heartbeat) > 60.0

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "node": self.node,
            "capabilities": [c.value for c in self.capabilities],
            "status": self.status.value,
            "available": self.available,
            "load_pct": round(self.load_pct, 1),
            "current_tasks": self.current_tasks,
            "max_concurrency": self.max_concurrency,
            "priority": self.priority,
            "failure_rate": round(self.failure_rate, 4),
            "total_tasks": self.total_tasks,
            "last_heartbeat": self.last_heartbeat,
        }


class AgentRegistry:
    def __init__(self, stale_timeout_s: float = 60.0):
        self.redis: aioredis.Redis | None = None
        self._agents: dict[str, AgentDescriptor] = {}
        self._stale_timeout = stale_timeout_s
        self._event_handlers: list[Callable] = []
        self._stats: dict[str, int] = {
            "registrations": 0,
            "deregistrations": 0,
            "heartbeats": 0,
            "task_assignments": 0,
            "stale_removed": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def on_event(self, handler: Callable):
        self._event_handlers.append(handler)

    def _emit(self, event_type: str, agent: AgentDescriptor):
        for h in self._event_handlers:
            try:
                h(event_type, agent)
            except Exception:
                pass
        if self.redis:
            asyncio.create_task(
                self.redis.publish(
                    REDIS_CHANNEL,
                    json.dumps(
                        {
                            "event": event_type,
                            "agent_id": agent.agent_id,
                            "name": agent.name,
                        }
                    ),
                )
            )

    def register(
        self,
        name: str,
        node: str,
        capabilities: list[AgentCapability],
        max_concurrency: int = 1,
        priority: int = 5,
        tags: list[str] | None = None,
        metadata: dict | None = None,
        agent_id: str | None = None,
    ) -> AgentDescriptor:
        aid = agent_id or str(uuid.uuid4())[:12]
        agent = AgentDescriptor(
            agent_id=aid,
            name=name,
            node=node,
            capabilities=capabilities,
            max_concurrency=max_concurrency,
            priority=priority,
            tags=tags or [],
            metadata=metadata or {},
        )
        self._agents[aid] = agent
        self._stats["registrations"] += 1
        self._emit("registered", agent)

        if self.redis:
            asyncio.create_task(
                self.redis.setex(
                    f"{REDIS_PREFIX}{aid}",
                    120,
                    json.dumps(agent.to_dict()),
                )
            )
        log.info(f"Agent registered: {name} ({aid}) on {node}")
        return agent

    def deregister(self, agent_id: str) -> bool:
        agent = self._agents.pop(agent_id, None)
        if agent:
            self._stats["deregistrations"] += 1
            self._emit("deregistered", agent)
            return True
        return False

    def heartbeat(self, agent_id: str, status: AgentStatus | None = None) -> bool:
        agent = self._agents.get(agent_id)
        if not agent:
            return False
        agent.last_heartbeat = time.time()
        if status:
            agent.status = status
        self._stats["heartbeats"] += 1
        return True

    def task_started(self, agent_id: str) -> bool:
        agent = self._agents.get(agent_id)
        if not agent or not agent.available:
            return False
        agent.current_tasks += 1
        agent.total_tasks += 1
        agent.status = (
            AgentStatus.BUSY
            if agent.current_tasks >= agent.max_concurrency
            else AgentStatus.IDLE
        )
        self._stats["task_assignments"] += 1
        return True

    def task_finished(self, agent_id: str, failed: bool = False) -> bool:
        agent = self._agents.get(agent_id)
        if not agent:
            return False
        agent.current_tasks = max(0, agent.current_tasks - 1)
        if failed:
            agent.failed_tasks += 1
        if agent.current_tasks == 0:
            agent.status = AgentStatus.IDLE
        return True

    def find(
        self,
        capability: AgentCapability | None = None,
        node: str | None = None,
        tags: list[str] | None = None,
        available_only: bool = True,
    ) -> list[AgentDescriptor]:
        agents = list(self._agents.values())

        if available_only:
            agents = [a for a in agents if a.available and not a.is_stale]
        if capability:
            agents = [a for a in agents if capability in a.capabilities]
        if node:
            agents = [a for a in agents if a.node == node]
        if tags:
            agents = [a for a in agents if any(t in a.tags for t in tags)]

        # Sort: priority desc, load asc, failure_rate asc
        agents.sort(key=lambda a: (-a.priority, a.load_pct, a.failure_rate))
        return agents

    def best_for(self, capability: AgentCapability) -> AgentDescriptor | None:
        candidates = self.find(capability, available_only=True)
        return candidates[0] if candidates else None

    def evict_stale(self):
        stale = [aid for aid, a in self._agents.items() if a.is_stale]
        for aid in stale:
            agent = self._agents.pop(aid)
            self._stats["stale_removed"] += 1
            self._emit("stale_removed", agent)
            log.warning(f"Stale agent evicted: {agent.name} ({aid})")

    def get(self, agent_id: str) -> AgentDescriptor | None:
        return self._agents.get(agent_id)

    def list_agents(self, status: AgentStatus | None = None) -> list[dict]:
        agents = list(self._agents.values())
        if status:
            agents = [a for a in agents if a.status == status]
        return [a.to_dict() for a in agents]

    def capability_map(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for agent in self._agents.values():
            for cap in agent.capabilities:
                result.setdefault(cap.value, []).append(agent.name)
        return result

    def stats(self) -> dict:
        by_status: dict[str, int] = {}
        for a in self._agents.values():
            by_status[a.status.value] = by_status.get(a.status.value, 0) + 1
        return {**self._stats, "total": len(self._agents), "by_status": by_status}


def build_jarvis_agent_registry() -> AgentRegistry:
    registry = AgentRegistry()

    def log_event(event_type: str, agent: AgentDescriptor):
        log.info(f"Agent event [{event_type}]: {agent.name} on {agent.node}")

    registry.on_event(log_event)

    # Pre-register known JARVIS agents
    registry.register(
        "openclaw-master",
        "local",
        [AgentCapability.ORCHESTRATION, AgentCapability.REASONING, AgentCapability.SEARCH],
        max_concurrency=10,
        priority=10,
        tags=["openclaw", "master"],
        metadata={"cli": "openclaw agent"}
    )
    registry.register(
        "inference-gateway",
        "m1",
        [AgentCapability.INFERENCE, AgentCapability.ORCHESTRATION],
        max_concurrency=8,
        priority=9,
    )
    registry.register(
        "gpu-scheduler",
        "m1",
        [AgentCapability.MONITORING],
        max_concurrency=1,
        priority=7,
    )
    registry.register(
        "embedding-worker",
        "m2",
        [AgentCapability.EMBEDDING],
        max_concurrency=4,
        priority=6,
    )
    registry.register(
        "reasoning-agent",
        "m2",
        [AgentCapability.REASONING, AgentCapability.INFERENCE],
        max_concurrency=2,
        priority=8,
    )
    registry.register(
        "browser-agent",
        "local",
        [AgentCapability.BROWSER, AgentCapability.SEARCH],
        max_concurrency=2,
        priority=5,
    )
    registry.register(
        "voice-agent", "ol1", [AgentCapability.VOICE], max_concurrency=1, priority=4
    )

    return registry


async def main():
    import sys

    registry = build_jarvis_agent_registry()
    await registry.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Registered agents:")
        for a in registry.list_agents():
            avail = "✅" if a["available"] else "🔒"
            caps = ", ".join(a["capabilities"])
            print(f"  {avail} {a['name']:<25} [{a['node']:<6}] caps={caps}")

        print("\nCapability map:")
        for cap, agents in sorted(registry.capability_map().items()):
            print(f"  {cap:<20} → {agents}")

        # Simulate task flow
        inf_agent = registry.best_for(AgentCapability.INFERENCE)
        if inf_agent:
            registry.task_started(inf_agent.agent_id)
            print(
                f"\nAssigned task to: {inf_agent.name} (load={inf_agent.load_pct:.0f}%)"
            )
            await asyncio.sleep(0.01)
            registry.task_finished(inf_agent.agent_id)
            print(f"Task done. Load now: {inf_agent.load_pct:.0f}%")

        print(f"\nStats: {json.dumps(registry.stats(), indent=2)}")

    elif cmd == "list":
        for a in registry.list_agents():
            print(f"  {a['agent_id']:<15} {a['name']:<25} {a['status']}")

    elif cmd == "stats":
        print(json.dumps(registry.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
