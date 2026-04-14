#!/usr/bin/env python3
"""
jarvis_distributed_lock — Redis-based distributed locking with deadlock prevention
Implements distributed mutex with SET NX PX, fencing tokens, and auto-expiry
"""

import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.distributed_lock")

REDIS_PREFIX = "jarvis:dlock:"
DEFAULT_TTL_S = 30.0
DEFAULT_RETRY_S = 0.1
DEFAULT_RETRY_COUNT = 50


@dataclass
class LockInfo:
    lock_id: str
    resource: str
    owner: str
    token: int  # fencing token — monotonically increasing
    acquired_at: float
    ttl_s: float
    expires_at: float

    @property
    def remaining_s(self) -> float:
        return max(0.0, self.expires_at - time.time())

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at

    def to_dict(self) -> dict:
        return {
            "lock_id": self.lock_id,
            "resource": self.resource,
            "owner": self.owner,
            "token": self.token,
            "acquired_at": self.acquired_at,
            "ttl_s": self.ttl_s,
            "remaining_s": round(self.remaining_s, 2),
            "expired": self.expired,
        }


class DistributedLock:
    def __init__(
        self,
        owner: str | None = None,
        retry_count: int = DEFAULT_RETRY_COUNT,
        retry_delay_s: float = DEFAULT_RETRY_S,
    ):
        self.redis: aioredis.Redis | None = None
        self.owner = owner or str(uuid.uuid4())[:8]
        self.retry_count = retry_count
        self.retry_delay_s = retry_delay_s
        self._held: dict[str, LockInfo] = {}
        self._stats: dict[str, int] = {
            "acquired": 0,
            "released": 0,
            "failed": 0,
            "extended": 0,
            "timeouts": 0,
        }

    async def connect_redis(self):
        self.redis = aioredis.Redis(decode_responses=True)
        await self.redis.ping()

    def _lock_key(self, resource: str) -> str:
        return f"{REDIS_PREFIX}{resource}"

    def _token_key(self, resource: str) -> str:
        return f"{REDIS_PREFIX}token:{resource}"

    def _lock_value(self, lock_id: str) -> str:
        return f"{self.owner}:{lock_id}"

    async def _atomic_acquire(
        self, lock_key: str, token_key: str, value: str, ttl_ms: int
    ) -> int:
        """Attempt SET NX then INCR token. Returns token on success, 0 on failure."""
        r = self.redis
        if not r:
            return 0
        # SET NX PX is atomic; INCR runs after to get a fencing token
        result = await r.set(lock_key, value, nx=True, px=ttl_ms)
        if result:
            token = await r.incr(token_key)
            return token
        return 0

    async def _atomic_release(self, lock_key: str, value: str) -> bool:
        """Release lock only if current owner (using WATCH + transaction)."""
        r = self.redis
        if not r:
            return False
        async with r.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(lock_key)
                current = await pipe.get(lock_key)
                if current != value:
                    await pipe.unwatch()
                    return False
                pipe.multi()
                pipe.delete(lock_key)
                await pipe.execute()
                return True
            except aioredis.WatchError:
                return False

    async def _atomic_extend(self, lock_key: str, value: str, ttl_ms: int) -> bool:
        """Extend TTL only if still owner."""
        r = self.redis
        if not r:
            return False
        async with r.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(lock_key)
                current = await pipe.get(lock_key)
                if current != value:
                    await pipe.unwatch()
                    return False
                pipe.multi()
                pipe.pexpire(lock_key, ttl_ms)
                await pipe.execute()
                return True
            except aioredis.WatchError:
                return False

    async def acquire(
        self,
        resource: str,
        ttl_s: float = DEFAULT_TTL_S,
        wait: bool = True,
    ) -> LockInfo | None:
        if not self.redis:
            raise RuntimeError("Redis not connected")

        lock_id = str(uuid.uuid4())[:10]
        value = self._lock_value(lock_id)
        ttl_ms = int(ttl_s * 1000)
        lock_key = self._lock_key(resource)
        token_key = self._token_key(resource)

        attempts = self.retry_count if wait else 1
        for attempt in range(attempts):
            try:
                token = await self._atomic_acquire(lock_key, token_key, value, ttl_ms)
                if token > 0:
                    now = time.time()
                    info = LockInfo(
                        lock_id=lock_id,
                        resource=resource,
                        owner=self.owner,
                        token=token,
                        acquired_at=now,
                        ttl_s=ttl_s,
                        expires_at=now + ttl_s,
                    )
                    self._held[resource] = info
                    self._stats["acquired"] += 1
                    log.debug(f"Lock acquired: {resource} token={token}")
                    return info
            except Exception as e:
                log.warning(f"Lock acquire error: {e}")

            if attempt < attempts - 1:
                await asyncio.sleep(self.retry_delay_s)

        self._stats["failed"] += 1
        self._stats["timeouts"] += 1
        return None

    async def release(self, resource: str) -> bool:
        if not self.redis:
            return False
        info = self._held.get(resource)
        if not info:
            return False

        value = self._lock_value(info.lock_id)
        released = await self._atomic_release(self._lock_key(resource), value)
        if released:
            del self._held[resource]
            self._stats["released"] += 1
            log.debug(f"Lock released: {resource}")
        return released

    async def extend(self, resource: str, additional_s: float = DEFAULT_TTL_S) -> bool:
        if not self.redis:
            return False
        info = self._held.get(resource)
        if not info or info.expired:
            return False

        value = self._lock_value(info.lock_id)
        ttl_ms = int(additional_s * 1000)
        extended = await self._atomic_extend(self._lock_key(resource), value, ttl_ms)
        if extended:
            info.expires_at = time.time() + additional_s
            info.ttl_s = additional_s
            self._stats["extended"] += 1
        return extended

    async def is_locked(self, resource: str) -> bool:
        if not self.redis:
            return False
        return await self.redis.exists(self._lock_key(resource)) == 1

    async def get_owner(self, resource: str) -> str | None:
        if not self.redis:
            return None
        val = await self.redis.get(self._lock_key(resource))
        return val.split(":")[0] if val else None

    @asynccontextmanager
    async def lock(
        self, resource: str, ttl_s: float = DEFAULT_TTL_S, wait: bool = True
    ) -> AsyncIterator[LockInfo | None]:
        info = await self.acquire(resource, ttl_s=ttl_s, wait=wait)
        try:
            yield info
        finally:
            if info:
                await self.release(resource)

    def held_locks(self) -> list[dict]:
        return [info.to_dict() for info in self._held.values()]

    def stats(self) -> dict:
        return {
            **self._stats,
            "currently_held": len(self._held),
            "success_rate": round(
                self._stats["acquired"]
                / max(self._stats["acquired"] + self._stats["failed"], 1)
                * 100,
                1,
            ),
        }


async def main():
    import sys

    dlock = DistributedLock(owner="jarvis-m1")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    try:
        await dlock.connect_redis()
    except Exception as e:
        print(f"Redis unavailable: {e}")
        print(json.dumps(dlock.stats(), indent=2))
        return

    if cmd == "demo":
        async with dlock.lock("jarvis:inference:slot-1", ttl_s=5.0) as info:
            if info:
                print(f"Lock acquired: token={info.token} ttl={info.ttl_s}s")
                print(f"  Owner: {info.owner}")
                print(f"  Remaining: {info.remaining_s:.1f}s")

                # Try to acquire again from another client (should fail)
                lock2 = DistributedLock(owner="other-node")
                await lock2.connect_redis()
                info2 = await lock2.acquire(
                    "jarvis:inference:slot-1", ttl_s=2.0, wait=False
                )
                print(f"Second acquire (no-wait): {'✅' if info2 else '❌ (expected)'}")
                if info2:
                    await lock2.release("jarvis:inference:slot-1")

                # Extend
                extended = await dlock.extend(
                    "jarvis:inference:slot-1", additional_s=10.0
                )
                print(f"Extend: {'✅' if extended else '❌'}")
            else:
                print("Failed to acquire lock")

        print(f"\nAfter context: held={len(dlock.held_locks())}")
        print(f"Stats: {json.dumps(dlock.stats(), indent=2)}")

    elif cmd == "check" and len(sys.argv) > 2:
        resource = sys.argv[2]
        locked = await dlock.is_locked(resource)
        owner = await dlock.get_owner(resource)
        print(f"{resource}: locked={locked} owner={owner}")

    elif cmd == "stats":
        print(json.dumps(dlock.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
