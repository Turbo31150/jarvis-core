#!/usr/bin/env python3
"""
jarvis_cluster_lease — Distributed lease/lock management for cluster coordination
TTL-based leases with renewal, fencing tokens, and Redis-backed persistence
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

log = logging.getLogger("jarvis.cluster_lease")

REDIS_PREFIX = "jarvis:lease:"


class LeaseState(str, Enum):
    HELD = "held"
    EXPIRED = "expired"
    RELEASED = "released"
    CONTESTED = "contested"


@dataclass
class Lease:
    lease_id: str
    resource: str
    holder: str  # node_id or agent_id
    fencing_token: int  # monotonically increasing per resource
    ttl_s: float
    acquired_at: float = field(default_factory=time.time)
    renewed_at: float = field(default_factory=time.time)
    state: LeaseState = LeaseState.HELD

    @property
    def expires_at(self) -> float:
        return self.renewed_at + self.ttl_s

    @property
    def expired(self) -> bool:
        return time.time() > self.expires_at

    @property
    def remaining_s(self) -> float:
        return max(0.0, self.expires_at - time.time())

    def to_dict(self) -> dict:
        return {
            "lease_id": self.lease_id,
            "resource": self.resource,
            "holder": self.holder,
            "fencing_token": self.fencing_token,
            "ttl_s": self.ttl_s,
            "acquired_at": self.acquired_at,
            "renewed_at": self.renewed_at,
            "expires_at": self.expires_at,
            "remaining_s": round(self.remaining_s, 1),
            "state": self.state.value,
        }


@dataclass
class LeaseResult:
    success: bool
    lease: Lease | None
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "lease": self.lease.to_dict() if self.lease else None,
            "reason": self.reason,
        }


class ClusterLease:
    def __init__(self, node_id: str = "node-local"):
        self.redis: aioredis.Redis | None = None
        self._node_id = node_id
        self._leases: dict[str, Lease] = {}  # resource → lease
        self._fencing_counters: dict[str, int] = {}  # resource → next token
        self._callbacks: list[Callable] = []  # called on expiry
        self._auto_renew_tasks: dict[str, asyncio.Task] = {}
        self._stats: dict[str, int] = {
            "acquired": 0,
            "renewed": 0,
            "released": 0,
            "expired": 0,
            "contested": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def on_expire(self, callback: Callable):
        """Register callback(lease) when a lease expires."""
        self._callbacks.append(callback)

    def _next_fencing_token(self, resource: str) -> int:
        token = self._fencing_counters.get(resource, 0) + 1
        self._fencing_counters[resource] = token
        return token

    async def acquire(
        self,
        resource: str,
        holder: str | None = None,
        ttl_s: float = 30.0,
        wait_s: float = 0.0,
    ) -> LeaseResult:
        """Try to acquire a lease. Optionally wait up to wait_s if contested."""
        holder = holder or self._node_id
        deadline = time.time() + wait_s

        while True:
            existing = self._leases.get(resource)
            if existing and not existing.expired and existing.state == LeaseState.HELD:
                if existing.holder == holder:
                    # Already held by us — renew
                    return await self.renew(resource, holder)
                self._stats["contested"] += 1
                if time.time() >= deadline:
                    return LeaseResult(
                        False,
                        None,
                        f"Resource '{resource}' held by '{existing.holder}' "
                        f"(expires in {existing.remaining_s:.1f}s)",
                    )
                await asyncio.sleep(0.2)
                continue

            # Acquire
            if existing and existing.expired:
                existing.state = LeaseState.EXPIRED
                self._stats["expired"] += 1
                for cb in self._callbacks:
                    try:
                        cb(existing)
                    except Exception:
                        pass

            token = self._next_fencing_token(resource)
            lease = Lease(
                lease_id=str(uuid.uuid4())[:12],
                resource=resource,
                holder=holder,
                fencing_token=token,
                ttl_s=ttl_s,
            )
            self._leases[resource] = lease
            self._stats["acquired"] += 1

            if self.redis:
                asyncio.create_task(self._redis_set(lease))

            log.info(
                f"Lease acquired: '{resource}' by '{holder}' "
                f"(token={token}, ttl={ttl_s}s)"
            )
            return LeaseResult(True, lease, "acquired")

    async def renew(self, resource: str, holder: str | None = None) -> LeaseResult:
        holder = holder or self._node_id
        lease = self._leases.get(resource)
        if not lease:
            return LeaseResult(False, None, f"No lease for '{resource}'")
        if lease.holder != holder:
            return LeaseResult(
                False, lease, f"Not the holder (held by '{lease.holder}')"
            )
        if lease.expired:
            lease.state = LeaseState.EXPIRED
            return LeaseResult(False, lease, "Lease already expired")

        lease.renewed_at = time.time()
        self._stats["renewed"] += 1

        if self.redis:
            asyncio.create_task(self._redis_set(lease))

        return LeaseResult(True, lease, "renewed")

    async def release(self, resource: str, holder: str | None = None) -> LeaseResult:
        holder = holder or self._node_id
        lease = self._leases.get(resource)
        if not lease:
            return LeaseResult(False, None, f"No lease for '{resource}'")
        if lease.holder != holder:
            return LeaseResult(False, lease, "Not the holder")

        lease.state = LeaseState.RELEASED
        del self._leases[resource]
        self._stats["released"] += 1

        # Cancel auto-renew if running
        task = self._auto_renew_tasks.pop(resource, None)
        if task:
            task.cancel()

        if self.redis:
            asyncio.create_task(self.redis.delete(f"{REDIS_PREFIX}{resource}"))

        log.info(f"Lease released: '{resource}' by '{holder}'")
        return LeaseResult(True, lease, "released")

    def start_auto_renew(
        self, resource: str, holder: str | None = None, interval_s: float | None = None
    ):
        """Start background renewal task."""

        async def _renew_loop():
            while True:
                lease = self._leases.get(resource)
                if not lease or lease.state != LeaseState.HELD:
                    break
                interval = interval_s or (lease.ttl_s * 0.4)
                await asyncio.sleep(interval)
                result = await self.renew(resource, holder)
                if not result.success:
                    log.warning(f"Auto-renew failed for '{resource}': {result.reason}")
                    break

        task = asyncio.create_task(_renew_loop())
        self._auto_renew_tasks[resource] = task

    def get_lease(self, resource: str) -> Lease | None:
        lease = self._leases.get(resource)
        if lease and lease.expired:
            lease.state = LeaseState.EXPIRED
        return lease

    def is_held_by(self, resource: str, holder: str | None = None) -> bool:
        holder = holder or self._node_id
        lease = self._leases.get(resource)
        return (
            lease is not None
            and lease.holder == holder
            and lease.state == LeaseState.HELD
            and not lease.expired
        )

    def purge_expired(self) -> int:
        expired = [
            res
            for res, lease in self._leases.items()
            if lease.expired and lease.state == LeaseState.HELD
        ]
        for res in expired:
            self._leases[res].state = LeaseState.EXPIRED
            self._stats["expired"] += 1
            for cb in self._callbacks:
                try:
                    cb(self._leases[res])
                except Exception:
                    pass
            del self._leases[res]
        return len(expired)

    async def _redis_set(self, lease: Lease):
        if not self.redis:
            return
        try:
            key = f"{REDIS_PREFIX}{lease.resource}"
            ttl = max(1, int(lease.ttl_s) + 5)
            await self.redis.setex(key, ttl, json.dumps(lease.to_dict()))
        except Exception as e:
            log.warning(f"Redis lease set error: {e}")

    def active_leases(self) -> list[dict]:
        self.purge_expired()
        return [l.to_dict() for l in self._leases.values()]

    def stats(self) -> dict:
        return {
            **self._stats,
            "active_leases": len(self._leases),
            "node_id": self._node_id,
        }


def build_jarvis_cluster_lease(node_id: str = "jarvis-core") -> ClusterLease:
    cl = ClusterLease(node_id=node_id)

    def log_expire(lease: Lease):
        log.warning(
            f"Lease expired: '{lease.resource}' was held by '{lease.holder}' "
            f"(token={lease.fencing_token})"
        )

    cl.on_expire(log_expire)
    return cl


async def main():
    import sys

    cl = build_jarvis_cluster_lease("m1")
    await cl.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        # Acquire leases
        r1 = await cl.acquire("model:deepseek-r1", ttl_s=10.0)
        print(
            f"Acquire model:deepseek-r1: {r1.success} token={r1.lease.fencing_token if r1.lease else None}"
        )

        r2 = await cl.acquire("cluster:leader", holder="m1", ttl_s=15.0)
        print(f"Acquire cluster:leader by m1: {r2.success}")

        # Contention
        r3 = await cl.acquire("cluster:leader", holder="m2", wait_s=0.0)
        print(
            f"Acquire cluster:leader by m2 (contested): {r3.success} reason={r3.reason}"
        )

        # Renew
        r4 = await cl.renew("model:deepseek-r1")
        print(f"Renew model:deepseek-r1: {r4.success}")

        # Check
        print(
            f"Is model:deepseek-r1 held by m1: {cl.is_held_by('model:deepseek-r1', 'm1')}"
        )

        # Release
        r5 = await cl.release("cluster:leader", "m1")
        print(f"Release cluster:leader by m1: {r5.success}")

        # Now m2 can acquire
        r6 = await cl.acquire("cluster:leader", holder="m2", ttl_s=15.0)
        print(
            f"Acquire cluster:leader by m2 (after release): {r6.success} token={r6.lease.fencing_token if r6.lease else None}"
        )

        print("\nActive leases:")
        for l in cl.active_leases():
            print(
                f"  {l['resource']:<25} holder={l['holder']:<8} "
                f"token={l['fencing_token']} remaining={l['remaining_s']}s"
            )

        print(f"\nStats: {json.dumps(cl.stats(), indent=2)}")

    elif cmd == "list":
        for l in cl.active_leases():
            print(json.dumps(l))

    elif cmd == "stats":
        print(json.dumps(cl.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
