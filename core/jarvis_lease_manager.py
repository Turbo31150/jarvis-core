#!/usr/bin/env python3
"""
jarvis_lease_manager — Distributed lease/lock management with TTL and renewal
Leader election, exclusive resource ownership, lease fencing tokens
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.lease_manager")

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
    holder: str
    token: int  # monotonically increasing fencing token
    ttl_s: float
    acquired_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    renewed_at: float = 0.0
    state: LeaseState = LeaseState.HELD
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.expires_at == 0.0:
            self.expires_at = self.acquired_at + self.ttl_s

    @property
    def is_expired(self) -> bool:
        return time.time() > self.expires_at

    @property
    def ttl_remaining(self) -> float:
        return max(0.0, self.expires_at - time.time())

    def to_dict(self) -> dict:
        return {
            "lease_id": self.lease_id,
            "resource": self.resource,
            "holder": self.holder,
            "token": self.token,
            "ttl_s": self.ttl_s,
            "acquired_at": self.acquired_at,
            "expires_at": self.expires_at,
            "ttl_remaining": round(self.ttl_remaining, 2),
            "state": self.state.value,
            "metadata": self.metadata,
        }


class LeaseManager:
    def __init__(self, default_ttl_s: float = 30.0):
        self.redis: aioredis.Redis | None = None
        self._leases: dict[str, Lease] = {}  # resource → lease
        self._fence_counters: dict[str, int] = {}  # resource → monotonic counter
        self._default_ttl = default_ttl_s
        self._renewal_tasks: dict[str, asyncio.Task] = {}
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

    def _next_token(self, resource: str) -> int:
        self._fence_counters[resource] = self._fence_counters.get(resource, 0) + 1
        return self._fence_counters[resource]

    async def acquire(
        self,
        resource: str,
        holder: str,
        ttl_s: float | None = None,
        metadata: dict | None = None,
    ) -> "Lease | None":
        import secrets

        ttl = ttl_s or self._default_ttl

        # Check existing lease
        existing = self._leases.get(resource)
        if existing and not existing.is_expired and existing.state == LeaseState.HELD:
            if existing.holder == holder:
                return await self.renew(resource, holder)
            self._stats["contested"] += 1
            log.debug(f"Lease for {resource!r} contested: held by {existing.holder!r}")
            return None

        # Try Redis SET NX EX
        if self.redis:
            lease_id = secrets.token_hex(8)
            token = self._next_token(resource)
            key = f"{REDIS_PREFIX}{resource}"
            data = json.dumps({"holder": holder, "token": token, "lease_id": lease_id})
            ok = await self.redis.set(key, data, nx=True, ex=int(ttl))
            if not ok:
                self._stats["contested"] += 1
                raw = await self.redis.get(key)
                if raw:
                    rd = json.loads(raw)
                    remote_lease = Lease(
                        lease_id=rd.get("lease_id", ""),
                        resource=resource,
                        holder=rd.get("holder", "?"),
                        token=rd.get("token", 0),
                        ttl_s=ttl,
                        state=LeaseState.HELD,
                    )
                    self._leases[resource] = remote_lease
                return None
        else:
            lease_id = secrets.token_hex(8)
            token = self._next_token(resource)

        lease = Lease(
            lease_id=lease_id,
            resource=resource,
            holder=holder,
            token=token,
            ttl_s=ttl,
            metadata=metadata or {},
        )
        self._leases[resource] = lease
        self._stats["acquired"] += 1
        log.info(
            f"Lease acquired: resource={resource!r} holder={holder!r} token={token} ttl={ttl}s"
        )
        return lease

    async def renew(
        self, resource: str, holder: str, extend_s: float | None = None
    ) -> "Lease | None":
        lease = self._leases.get(resource)
        if not lease or lease.holder != holder:
            return None
        if lease.is_expired:
            lease.state = LeaseState.EXPIRED
            self._stats["expired"] += 1
            return None

        extend = extend_s or lease.ttl_s
        lease.expires_at = time.time() + extend
        lease.renewed_at = time.time()
        self._stats["renewed"] += 1

        if self.redis:
            key = f"{REDIS_PREFIX}{resource}"
            data = json.dumps(
                {"holder": holder, "token": lease.token, "lease_id": lease.lease_id}
            )
            await self.redis.set(key, data, ex=int(extend))

        log.debug(
            f"Lease renewed: resource={resource!r} ttl_remaining={lease.ttl_remaining:.1f}s"
        )
        return lease

    async def release(self, resource: str, holder: str) -> bool:
        lease = self._leases.get(resource)
        if not lease or lease.holder != holder:
            return False

        lease.state = LeaseState.RELEASED
        del self._leases[resource]
        self._stats["released"] += 1

        if self.redis:
            # Only delete if still our holder (GET + DEL — best-effort, not atomic)
            key = f"{REDIS_PREFIX}{resource}"
            raw = await self.redis.get(key)
            if raw:
                rd = json.loads(raw)
                if rd.get("holder") == holder:
                    await self.redis.delete(key)

        log.info(f"Lease released: resource={resource!r} holder={holder!r}")
        return True

    async def start_auto_renewal(
        self, resource: str, holder: str, interval_s: float | None = None
    ):
        lease = self._leases.get(resource)
        if not lease or lease.holder != holder:
            return
        interval = interval_s or (lease.ttl_s / 2)

        async def _renew_loop():
            while True:
                await asyncio.sleep(interval)
                current = self._leases.get(resource)
                if (
                    not current
                    or current.holder != holder
                    or current.state != LeaseState.HELD
                ):
                    break
                renewed = await self.renew(resource, holder)
                if not renewed:
                    log.warning(f"Auto-renewal failed for {resource!r}")
                    break

        task = asyncio.create_task(_renew_loop())
        self._renewal_tasks[resource] = task

    def stop_auto_renewal(self, resource: str):
        task = self._renewal_tasks.pop(resource, None)
        if task:
            task.cancel()

    def is_held_by(self, resource: str, holder: str) -> bool:
        lease = self._leases.get(resource)
        if not lease:
            return False
        return (
            lease.holder == holder
            and not lease.is_expired
            and lease.state == LeaseState.HELD
        )

    def get_lease(self, resource: str) -> "Lease | None":
        lease = self._leases.get(resource)
        if lease and lease.is_expired:
            lease.state = LeaseState.EXPIRED
            self._stats["expired"] += 1
        return lease

    def purge_expired(self) -> int:
        expired = [r for r, l in self._leases.items() if l.is_expired]
        for r in expired:
            self._leases[r].state = LeaseState.EXPIRED
            del self._leases[r]
            self._stats["expired"] += 1
        return len(expired)

    def list_held(self) -> list[dict]:
        self.purge_expired()
        return [
            l.to_dict() for l in self._leases.values() if l.state == LeaseState.HELD
        ]

    def stats(self) -> dict:
        self.purge_expired()
        return {
            **self._stats,
            "active_leases": len(self._leases),
            "auto_renewals": len(self._renewal_tasks),
        }


def build_jarvis_lease_manager() -> LeaseManager:
    return LeaseManager(default_ttl_s=30.0)


async def main():
    import sys

    lm = build_jarvis_lease_manager()
    await lm.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Lease manager demo...")

        l1 = await lm.acquire("gpu:m1:slot0", "inference-gw", ttl_s=10.0)
        l2 = await lm.acquire("trading:executor", "trading-agent", ttl_s=5.0)
        l3 = await lm.acquire("gpu:m1:slot0", "other-agent")

        print(
            f"\n  inference-gw  → {'OK token=' + str(l1.token) if l1 else 'CONTESTED'}"
        )
        print(f"  trading-agent → {'OK token=' + str(l2.token) if l2 else 'CONTESTED'}")
        print(f"  other-agent   → {'OK' if l3 else 'CONTESTED (expected)'}")

        r = await lm.renew("gpu:m1:slot0", "inference-gw")
        if r:
            print(f"\n  Renewed gpu:m1:slot0 → ttl_remaining={r.ttl_remaining:.1f}s")

        print(
            f"\n  is_held_by inference-gw  → {lm.is_held_by('gpu:m1:slot0', 'inference-gw')}"
        )
        print(
            f"  is_held_by other-agent   → {lm.is_held_by('gpu:m1:slot0', 'other-agent')}"
        )

        ok = await lm.release("gpu:m1:slot0", "inference-gw")
        print(f"\n  Released gpu:m1:slot0 → {ok}")
        l4 = await lm.acquire("gpu:m1:slot0", "other-agent")
        print(
            f"  other-agent now acquires → {'OK token=' + str(l4.token) if l4 else 'FAILED'}"
        )

        print(f"\nStats: {json.dumps(lm.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(lm.stats(), indent=2))

    elif cmd == "list":
        for l in lm.list_held():
            print(
                f"  {l['resource']:<30} holder={l['holder']:<20} ttl={l['ttl_remaining']:.1f}s"
            )


if __name__ == "__main__":
    asyncio.run(main())
