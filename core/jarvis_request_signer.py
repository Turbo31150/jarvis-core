#!/usr/bin/env python3
"""
jarvis_request_signer — HMAC-based request signing and verification for inter-agent calls
Signs outgoing requests, verifies incoming signatures, prevents replay attacks
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.request_signer")

REDIS_PREFIX = "jarvis:signer:"
NONCE_TTL_S = 300  # 5 min replay window
MAX_CLOCK_SKEW_S = 60.0  # reject requests older than 60s


class SigningAlgo(str, Enum):
    HMAC_SHA256 = "hmac-sha256"
    HMAC_SHA512 = "hmac-sha512"


@dataclass
class SigningKey:
    key_id: str
    secret: str
    algo: SigningAlgo = SigningAlgo.HMAC_SHA256
    created_at: float = field(default_factory=time.time)
    last_used: float = 0.0
    use_count: int = 0

    def to_dict(self) -> dict:
        return {
            "key_id": self.key_id,
            "algo": self.algo.value,
            "created_at": self.created_at,
            "last_used": self.last_used,
            "use_count": self.use_count,
        }


@dataclass
class SignedRequest:
    payload: dict
    signature: str
    key_id: str
    nonce: str
    timestamp: float
    algo: SigningAlgo = SigningAlgo.HMAC_SHA256

    def to_headers(self) -> dict[str, str]:
        return {
            "X-Jarvis-Key-ID": self.key_id,
            "X-Jarvis-Signature": self.signature,
            "X-Jarvis-Nonce": self.nonce,
            "X-Jarvis-Timestamp": str(self.timestamp),
            "X-Jarvis-Algo": self.algo.value,
        }

    @classmethod
    def from_headers(cls, payload: dict, headers: dict) -> "SignedRequest":
        return cls(
            payload=payload,
            signature=headers.get("X-Jarvis-Signature", ""),
            key_id=headers.get("X-Jarvis-Key-ID", ""),
            nonce=headers.get("X-Jarvis-Nonce", ""),
            timestamp=float(headers.get("X-Jarvis-Timestamp", "0")),
            algo=SigningAlgo(headers.get("X-Jarvis-Algo", "hmac-sha256")),
        )


@dataclass
class VerifyResult:
    valid: bool
    reason: str = ""
    key_id: str = ""
    age_s: float = 0.0

    def to_dict(self) -> dict:
        return {
            "valid": self.valid,
            "reason": self.reason,
            "key_id": self.key_id,
            "age_s": round(self.age_s, 2),
        }


def _canonical_payload(payload: dict) -> str:
    """Deterministic JSON serialization for signing."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _compute_signature(
    secret: str,
    nonce: str,
    timestamp: float,
    canonical: str,
    algo: SigningAlgo,
) -> str:
    message = f"{nonce}:{timestamp:.6f}:{canonical}"
    key_bytes = secret.encode()
    msg_bytes = message.encode()
    if algo == SigningAlgo.HMAC_SHA512:
        return hmac.new(key_bytes, msg_bytes, hashlib.sha512).hexdigest()
    return hmac.new(key_bytes, msg_bytes, hashlib.sha256).hexdigest()


class RequestSigner:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._keys: dict[str, SigningKey] = {}
        self._used_nonces: set[str] = set()  # in-memory anti-replay
        self._stats: dict[str, int] = {
            "signed": 0,
            "verified": 0,
            "rejected_sig": 0,
            "rejected_replay": 0,
            "rejected_expired": 0,
            "rejected_unknown_key": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def add_key(
        self, key_id: str, secret: str, algo: SigningAlgo = SigningAlgo.HMAC_SHA256
    ) -> SigningKey:
        key = SigningKey(key_id=key_id, secret=secret, algo=algo)
        self._keys[key_id] = key
        log.debug(f"Signing key registered: {key_id}")
        return key

    def remove_key(self, key_id: str):
        self._keys.pop(key_id, None)

    def sign(self, payload: dict, key_id: str) -> SignedRequest:
        key = self._keys.get(key_id)
        if not key:
            raise ValueError(f"Unknown key_id: {key_id}")

        nonce = str(uuid.uuid4())
        timestamp = time.time()
        canonical = _canonical_payload(payload)
        signature = _compute_signature(
            key.secret, nonce, timestamp, canonical, key.algo
        )

        key.last_used = timestamp
        key.use_count += 1
        self._stats["signed"] += 1

        return SignedRequest(
            payload=payload,
            signature=signature,
            key_id=key_id,
            nonce=nonce,
            timestamp=timestamp,
            algo=key.algo,
        )

    async def _check_nonce(self, nonce: str) -> bool:
        """Returns True if nonce is fresh (not replayed)."""
        # Check in-memory first
        if nonce in self._used_nonces:
            return False

        # Check Redis for distributed replay protection
        if self.redis:
            redis_key = f"{REDIS_PREFIX}nonce:{nonce}"
            exists = await self.redis.exists(redis_key)
            if exists:
                return False
            await self.redis.setex(redis_key, NONCE_TTL_S, "1")

        self._used_nonces.add(nonce)
        # Prune in-memory nonces to prevent unbounded growth
        if len(self._used_nonces) > 10000:
            self._used_nonces.clear()
        return True

    async def verify(self, signed: SignedRequest) -> VerifyResult:
        self._stats["verified"] += 1
        now = time.time()
        age = now - signed.timestamp

        # Clock skew check
        if abs(age) > MAX_CLOCK_SKEW_S:
            self._stats["rejected_expired"] += 1
            return VerifyResult(
                False, f"Request expired: age={age:.1f}s > {MAX_CLOCK_SKEW_S}s"
            )

        # Key lookup
        key = self._keys.get(signed.key_id)
        if not key:
            self._stats["rejected_unknown_key"] += 1
            return VerifyResult(False, f"Unknown key_id: {signed.key_id}")

        # Signature verification
        canonical = _canonical_payload(signed.payload)
        expected = _compute_signature(
            key.secret, signed.nonce, signed.timestamp, canonical, signed.algo
        )
        if not hmac.compare_digest(expected, signed.signature):
            self._stats["rejected_sig"] += 1
            return VerifyResult(False, "Invalid signature", signed.key_id, age)

        # Replay check
        fresh = await self._check_nonce(signed.nonce)
        if not fresh:
            self._stats["rejected_replay"] += 1
            return VerifyResult(
                False, "Replay detected: nonce already used", signed.key_id, age
            )

        return VerifyResult(True, "OK", signed.key_id, age)

    def verify_sync(self, signed: SignedRequest) -> VerifyResult:
        """Sync verification (no Redis replay check)."""
        self._stats["verified"] += 1
        now = time.time()
        age = now - signed.timestamp

        if abs(age) > MAX_CLOCK_SKEW_S:
            self._stats["rejected_expired"] += 1
            return VerifyResult(False, f"Expired: age={age:.1f}s")

        key = self._keys.get(signed.key_id)
        if not key:
            self._stats["rejected_unknown_key"] += 1
            return VerifyResult(False, f"Unknown key: {signed.key_id}")

        canonical = _canonical_payload(signed.payload)
        expected = _compute_signature(
            key.secret, signed.nonce, signed.timestamp, canonical, signed.algo
        )
        if not hmac.compare_digest(expected, signed.signature):
            self._stats["rejected_sig"] += 1
            return VerifyResult(False, "Invalid signature", signed.key_id, age)

        if signed.nonce in self._used_nonces:
            self._stats["rejected_replay"] += 1
            return VerifyResult(False, "Replay detected", signed.key_id, age)

        self._used_nonces.add(signed.nonce)
        return VerifyResult(True, "OK", signed.key_id, age)

    def list_keys(self) -> list[dict]:
        return [k.to_dict() for k in self._keys.values()]

    def stats(self) -> dict:
        return {**self._stats, "keys": len(self._keys)}


def build_jarvis_signer() -> RequestSigner:
    signer = RequestSigner()
    signer.add_key("jarvis-internal", "jarvis-secret-key-v1-do-not-share")
    signer.add_key("agent-m1", "m1-agent-secret-2026")
    signer.add_key("agent-m2", "m2-agent-secret-2026")
    return signer


async def main():
    import sys

    signer = build_jarvis_signer()
    await signer.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        payload = {
            "action": "infer",
            "model": "qwen3.5-9b",
            "messages": [{"role": "user", "content": "Hello"}],
        }

        # Sign
        signed = signer.sign(payload, "jarvis-internal")
        print("Signed request:")
        print(f"  key_id={signed.key_id}")
        print(f"  nonce={signed.nonce}")
        print(f"  signature={signed.signature[:32]}...")
        headers = signed.to_headers()
        print(f"  headers: {list(headers.keys())}")

        # Verify (valid)
        result = await signer.verify(signed)
        print(f"\nVerify valid: {result.valid} — {result.reason}")

        # Verify replay
        result2 = await signer.verify(signed)
        print(f"Verify replay: {result2.valid} — {result2.reason}")

        # Tampered payload
        tampered = SignedRequest(
            payload={"action": "delete_all"},
            signature=signed.signature,
            key_id=signed.key_id,
            nonce=str(uuid.uuid4()),
            timestamp=time.time(),
        )
        result3 = await signer.verify(tampered)
        print(f"Verify tampered: {result3.valid} — {result3.reason}")

        print(f"\nStats: {json.dumps(signer.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(signer.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
