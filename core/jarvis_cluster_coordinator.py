#!/usr/bin/env python3
"""
jarvis_cluster_coordinator — Multi-node cluster coordination and consensus
Leader election, node heartbeats, distributed task assignment, split-brain prevention
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.cluster_coordinator")

REDIS_PREFIX = "jarvis:cluster:"
HEARTBEAT_INTERVAL_S = 5.0
NODE_TIMEOUT_S = 15.0
ELECTION_TTL_S = 10.0

KNOWN_NODES = {
    "M1": {"host": "127.0.0.1", "api_port": 1234, "coord_port": 8771},
    "M2": {"host": "192.168.1.26", "api_port": 1234, "coord_port": 8771},
}


class NodeStatus(str, Enum):
    ALIVE = "alive"
    SUSPECT = "suspect"
    DEAD = "dead"
    UNKNOWN = "unknown"


@dataclass
class NodeInfo:
    node_id: str
    host: str
    api_port: int
    status: NodeStatus = NodeStatus.UNKNOWN
    last_heartbeat: float = 0.0
    is_leader: bool = False
    load: float = 0.0  # 0-1 normalized load
    models_loaded: int = 0
    tasks_active: int = 0

    @property
    def alive(self) -> bool:
        return self.status == NodeStatus.ALIVE

    @property
    def age_s(self) -> float:
        return time.time() - self.last_heartbeat if self.last_heartbeat else 9999.0

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "host": self.host,
            "api_port": self.api_port,
            "status": self.status.value,
            "last_heartbeat": self.last_heartbeat,
            "age_s": round(self.age_s, 1),
            "is_leader": self.is_leader,
            "load": round(self.load, 3),
            "models_loaded": self.models_loaded,
            "tasks_active": self.tasks_active,
        }


@dataclass
class ClusterTask:
    task_id: str
    task_type: str
    payload: dict
    assigned_node: str = ""
    status: str = "pending"  # pending | running | done | failed
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    ended_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "payload": self.payload,
            "assigned_node": self.assigned_node,
            "status": self.status,
            "created_at": self.created_at,
        }


class ClusterCoordinator:
    def __init__(self, node_id: str = "M1"):
        self.node_id = node_id
        self.redis: aioredis.Redis | None = None
        self._nodes: dict[str, NodeInfo] = {}
        self._is_leader = False
        self._tasks: dict[str, ClusterTask] = {}
        self._heartbeat_task: asyncio.Task | None = None
        self._monitor_task: asyncio.Task | None = None
        self._init_nodes()

    def _init_nodes(self):
        for nid, cfg in KNOWN_NODES.items():
            self._nodes[nid] = NodeInfo(
                node_id=nid,
                host=cfg["host"],
                api_port=cfg["api_port"],
            )

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    # ── Leader election ────────────────────────────────────────────────────────

    async def try_become_leader(self) -> bool:
        """Attempt to acquire leadership via Redis SETNX."""
        if not self.redis:
            self._is_leader = True
            return True

        acquired = await self.redis.set(
            f"{REDIS_PREFIX}leader",
            self.node_id,
            nx=True,
            ex=int(ELECTION_TTL_S),
        )
        if acquired:
            self._is_leader = True
            log.info(f"[{self.node_id}] Became LEADER")
            await self.redis.publish(
                "jarvis:events",
                json.dumps({"event": "leader_elected", "node": self.node_id}),
            )
            return True
        return False

    async def renew_leadership(self) -> bool:
        """Renew leader TTL if we are the current leader."""
        if not self.redis or not self._is_leader:
            return False
        current = await self.redis.get(f"{REDIS_PREFIX}leader")
        if current == self.node_id:
            await self.redis.expire(f"{REDIS_PREFIX}leader", int(ELECTION_TTL_S))
            return True
        self._is_leader = False
        return False

    async def get_leader(self) -> str | None:
        if not self.redis:
            return self.node_id
        return await self.redis.get(f"{REDIS_PREFIX}leader")

    # ── Heartbeat ──────────────────────────────────────────────────────────────

    async def send_heartbeat(self, load: float = 0.0, models: int = 0, tasks: int = 0):
        if not self.redis:
            return
        data = {
            "node_id": self.node_id,
            "ts": time.time(),
            "is_leader": self._is_leader,
            "load": load,
            "models_loaded": models,
            "tasks_active": tasks,
        }
        await self.redis.setex(
            f"{REDIS_PREFIX}hb:{self.node_id}",
            int(NODE_TIMEOUT_S * 2),
            json.dumps(data),
        )
        await self.redis.hset(f"{REDIS_PREFIX}nodes", self.node_id, json.dumps(data))

    async def refresh_nodes(self):
        """Pull latest node states from Redis."""
        if not self.redis:
            return
        raw = await self.redis.hgetall(f"{REDIS_PREFIX}nodes")
        for nid, data_str in raw.items():
            try:
                d = json.loads(data_str)
                node = self._nodes.get(nid) or NodeInfo(
                    node_id=nid, host="unknown", api_port=1234
                )
                node.last_heartbeat = d.get("ts", 0)
                node.is_leader = d.get("is_leader", False)
                node.load = d.get("load", 0.0)
                node.models_loaded = d.get("models_loaded", 0)
                node.tasks_active = d.get("tasks_active", 0)
                age = time.time() - node.last_heartbeat
                if age < NODE_TIMEOUT_S:
                    node.status = NodeStatus.ALIVE
                elif age < NODE_TIMEOUT_S * 2:
                    node.status = NodeStatus.SUSPECT
                else:
                    node.status = NodeStatus.DEAD
                self._nodes[nid] = node
            except Exception:
                pass

    async def start_heartbeat_loop(self):
        async def loop():
            while True:
                try:
                    if self._is_leader:
                        await self.renew_leadership()
                    else:
                        await self.try_become_leader()
                    await self.send_heartbeat()
                    await self.refresh_nodes()
                except Exception as e:
                    log.error(f"Heartbeat error: {e}")
                await asyncio.sleep(HEARTBEAT_INTERVAL_S)

        self._heartbeat_task = asyncio.create_task(loop())

    # ── Task assignment ────────────────────────────────────────────────────────

    def assign_task(self, task_type: str, payload: dict) -> ClusterTask:
        """Assign task to least-loaded alive node."""
        alive = [n for n in self._nodes.values() if n.alive]
        if not alive:
            alive = list(self._nodes.values())

        target = min(alive, key=lambda n: n.load + n.tasks_active * 0.1)
        task = ClusterTask(
            task_id=str(uuid.uuid4())[:8],
            task_type=task_type,
            payload=payload,
            assigned_node=target.node_id,
        )
        self._tasks[task.task_id] = task
        target.tasks_active += 1
        log.info(f"Task [{task.task_id}] {task_type} → {target.node_id}")
        return task

    # ── Cluster state ──────────────────────────────────────────────────────────

    async def probe_node(self, node_id: str) -> bool:
        """Direct HTTP probe to check node availability."""
        node = self._nodes.get(node_id)
        if not node:
            return False
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=3)
            ) as sess:
                async with sess.get(
                    f"http://{node.host}:{node.api_port}/v1/models"
                ) as r:
                    if r.status == 200:
                        node.status = NodeStatus.ALIVE
                        node.last_heartbeat = time.time()
                        return True
        except Exception:
            pass
        node.status = NodeStatus.DEAD
        return False

    async def probe_all(self) -> dict[str, bool]:
        results = {}
        for nid in self._nodes:
            results[nid] = await self.probe_node(nid)
        return results

    def cluster_status(self) -> dict:
        alive = [n for n in self._nodes.values() if n.alive]
        return {
            "node_id": self.node_id,
            "is_leader": self._is_leader,
            "total_nodes": len(self._nodes),
            "alive_nodes": len(alive),
            "nodes": [n.to_dict() for n in self._nodes.values()],
            "tasks_pending": sum(
                1 for t in self._tasks.values() if t.status == "pending"
            ),
            "tasks_running": sum(
                1 for t in self._tasks.values() if t.status == "running"
            ),
        }


async def main():
    import sys

    coord = ClusterCoordinator(node_id="M1")
    await coord.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "status":
        await coord.refresh_nodes()
        s = coord.cluster_status()
        leader = await coord.get_leader()
        print(
            f"Cluster: {s['alive_nodes']}/{s['total_nodes']} alive | Leader: {leader}"
        )
        print(f"{'Node':<6} {'Status':<10} {'Leader':<8} {'Load':>6} {'Age':>6}")
        print("-" * 45)
        for n in s["nodes"]:
            ldr = "👑" if n["is_leader"] else "  "
            print(
                f"  {n['node_id']:<6} {n['status']:<10} {ldr:<8} {n['load']:>5.1%} {n['age_s']:>5.0f}s"
            )

    elif cmd == "probe":
        results = await coord.probe_all()
        for nid, alive in results.items():
            print(f"  {'✅' if alive else '❌'} {nid}")

    elif cmd == "elect":
        ok = await coord.try_become_leader()
        print(f"Leader election: {'✅ won' if ok else '❌ lost'}")

    elif cmd == "assign" and len(sys.argv) > 2:
        task = coord.assign_task(sys.argv[2], {})
        print(f"Task [{task.task_id}] → {task.assigned_node}")


if __name__ == "__main__":
    asyncio.run(main())
