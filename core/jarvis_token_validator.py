#!/usr/bin/env python3
"""
jarvis_token_validator — JWT and API token validation with HMAC/RSA support
Token issuance, verification, refresh, revocation via Redis blocklist
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.token_validator")

REDIS_PREFIX = "jarvis:token:"
BLOCKLIST_PREFIX = "jarvis:token:bl:"
DEFAULT_ALGORITHM = "HS256"


class TokenType(str, Enum):
    ACCESS = "access"
    REFRESH = "refresh"
    API_KEY = "api_key"
    SERVICE = "service"


class TokenStatus(str, Enum):
    VALID = "valid"
    EXPIRED = "expired"
    REVOKED = "revoked"
    INVALID_SIGNATURE = "invalid_signature"
    MALFORMED = "malformed"
    UNKNOWN = "unknown"


@dataclass
class TokenClaims:
    sub: str  # subject (principal_id)
    iss: str  # issuer
    token_type: TokenType
    iat: float  # issued at
    exp: float  # expiry
    jti: str  # token ID (unique)
    roles: list[str] = field(default_factory=list)
    scopes: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_expired(self) -> bool:
        return time.time() > self.exp

    @property
    def ttl(self) -> float:
        return max(0.0, self.exp - time.time())

    def to_dict(self) -> dict:
        return {
            "sub": self.sub,
            "iss": self.iss,
            "type": self.token_type.value,
            "iat": self.iat,
            "exp": self.exp,
            "jti": self.jti,
            "roles": self.roles,
            "scopes": self.scopes,
            **self.extra,
        }


@dataclass
class ValidationResult:
    status: TokenStatus
    claims: TokenClaims | None = None
    reason: str = ""
    token_id: str = ""

    @property
    def ok(self) -> bool:
        return self.status == TokenStatus.VALID

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "ok": self.ok,
            "reason": self.reason,
            "token_id": self.token_id,
            "claims": self.claims.to_dict() if self.claims else None,
        }


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    pad = 4 - len(s) % 4
    if pad != 4:
        s += "=" * pad
    return base64.urlsafe_b64decode(s)


def _hs256_sign(header_b64: str, payload_b64: str, secret: bytes) -> str:
    msg = f"{header_b64}.{payload_b64}".encode()
    sig = hmac.new(secret, msg, hashlib.sha256).digest()
    return _b64url_encode(sig)


def _issue_jwt(claims: TokenClaims, secret: bytes) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    h = _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    p = _b64url_encode(json.dumps(claims.to_dict(), separators=(",", ":")).encode())
    sig = _hs256_sign(h, p, secret)
    return f"{h}.{p}.{sig}"


def _parse_jwt(token: str) -> tuple[dict, dict, str] | None:
    """Returns (header, payload, signature_b64) or None if malformed."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        header = json.loads(_b64url_decode(parts[0]))
        payload = json.loads(_b64url_decode(parts[1]))
        return header, payload, parts[2]
    except Exception:
        return None


def _verify_jwt(token: str, secret: bytes) -> tuple[bool, dict | None]:
    """Returns (valid, payload_dict)."""
    parsed = _parse_jwt(token)
    if not parsed:
        return False, None
    header, payload, provided_sig = parsed
    parts = token.split(".")
    expected_sig = _hs256_sign(parts[0], parts[1], secret)
    if not hmac.compare_digest(expected_sig, provided_sig):
        return False, None
    return True, payload


class TokenValidator:
    def __init__(self, issuer: str = "jarvis", secret: str | None = None):
        self.redis: aioredis.Redis | None = None
        self._issuer = issuer
        self._secret: bytes = (secret or secrets.token_hex(32)).encode()
        self._revoked: set[str] = set()  # local blocklist cache
        self._stats: dict[str, int] = {
            "issued": 0,
            "validated": 0,
            "valid": 0,
            "expired": 0,
            "revoked": 0,
            "invalid": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def issue(
        self,
        subject: str,
        token_type: TokenType = TokenType.ACCESS,
        ttl_s: float = 3600.0,
        roles: list[str] | None = None,
        scopes: list[str] | None = None,
        extra: dict | None = None,
    ) -> str:
        now = time.time()
        claims = TokenClaims(
            sub=subject,
            iss=self._issuer,
            token_type=token_type,
            iat=now,
            exp=now + ttl_s,
            jti=secrets.token_hex(16),
            roles=roles or [],
            scopes=scopes or [],
            extra=extra or {},
        )
        token = _issue_jwt(claims, self._secret)
        self._stats["issued"] += 1
        log.debug(f"Issued {token_type.value} token for {subject!r} jti={claims.jti}")
        return token

    def issue_api_key(
        self,
        subject: str,
        scopes: list[str] | None = None,
        ttl_s: float = 86400.0 * 365,
    ) -> str:
        return self.issue(subject, TokenType.API_KEY, ttl_s, scopes=scopes)

    def validate(self, token: str) -> ValidationResult:
        self._stats["validated"] += 1

        if not token or not isinstance(token, str):
            self._stats["invalid"] += 1
            return ValidationResult(
                TokenStatus.MALFORMED, reason="empty or non-string token"
            )

        # Parse
        parsed = _parse_jwt(token)
        if not parsed:
            self._stats["invalid"] += 1
            return ValidationResult(
                TokenStatus.MALFORMED, reason="not a valid JWT structure"
            )

        # Signature
        valid_sig, payload = _verify_jwt(token, self._secret)
        if not valid_sig or payload is None:
            self._stats["invalid"] += 1
            return ValidationResult(
                TokenStatus.INVALID_SIGNATURE, reason="signature mismatch"
            )

        # Build claims
        try:
            claims = TokenClaims(
                sub=payload["sub"],
                iss=payload.get("iss", ""),
                token_type=TokenType(payload.get("type", "access")),
                iat=float(payload.get("iat", 0)),
                exp=float(payload.get("exp", 0)),
                jti=payload.get("jti", ""),
                roles=payload.get("roles", []),
                scopes=payload.get("scopes", []),
            )
        except Exception as e:
            self._stats["invalid"] += 1
            return ValidationResult(
                TokenStatus.MALFORMED, reason=f"payload parse error: {e}"
            )

        jti = claims.jti

        # Expiry
        if claims.is_expired:
            self._stats["expired"] += 1
            return ValidationResult(
                TokenStatus.EXPIRED,
                claims=claims,
                reason=f"expired {time.time() - claims.exp:.0f}s ago",
                token_id=jti,
            )

        # Revocation (local cache)
        if jti in self._revoked:
            self._stats["revoked"] += 1
            return ValidationResult(
                TokenStatus.REVOKED, claims=claims, reason="token revoked", token_id=jti
            )

        self._stats["valid"] += 1
        return ValidationResult(TokenStatus.VALID, claims=claims, token_id=jti)

    async def validate_async(self, token: str) -> ValidationResult:
        result = self.validate(token)
        if result.status == TokenStatus.VALID and self.redis and result.token_id:
            try:
                blocked = await self.redis.exists(
                    f"{BLOCKLIST_PREFIX}{result.token_id}"
                )
                if blocked:
                    self._revoked.add(result.token_id)
                    self._stats["valid"] -= 1
                    self._stats["revoked"] += 1
                    return ValidationResult(
                        TokenStatus.REVOKED,
                        claims=result.claims,
                        reason="token revoked (redis blocklist)",
                        token_id=result.token_id,
                    )
            except Exception:
                pass
        return result

    def revoke(self, token: str) -> bool:
        result = self.validate(token)
        if not result.token_id:
            return False
        self._revoked.add(result.token_id)
        if self.redis and result.claims:
            asyncio.create_task(
                self._redis_revoke(result.token_id, result.claims.ttl + 60)
            )
        log.info(f"Revoked token jti={result.token_id}")
        return True

    async def _redis_revoke(self, jti: str, ttl_s: float):
        if not self.redis:
            return
        try:
            await self.redis.setex(f"{BLOCKLIST_PREFIX}{jti}", int(ttl_s), "1")
        except Exception:
            pass

    def refresh(self, token: str, new_ttl_s: float = 3600.0) -> str | None:
        result = self.validate(token)
        if result.status not in (TokenStatus.VALID, TokenStatus.EXPIRED):
            return None
        if result.claims is None:
            return None
        # Revoke old
        if result.token_id:
            self._revoked.add(result.token_id)
        # Issue new
        c = result.claims
        return self.issue(c.sub, c.token_type, new_ttl_s, c.roles, c.scopes, c.extra)

    def has_scope(self, token: str, scope: str) -> bool:
        result = self.validate(token)
        if not result.ok or result.claims is None:
            return False
        return scope in result.claims.scopes

    def has_role(self, token: str, role: str) -> bool:
        result = self.validate(token)
        if not result.ok or result.claims is None:
            return False
        return role in result.claims.roles

    def stats(self) -> dict:
        return {
            **self._stats,
            "revoked_cached": len(self._revoked),
            "valid_rate": round(
                self._stats["valid"] / max(self._stats["validated"], 1), 4
            ),
        }


def build_jarvis_token_validator() -> TokenValidator:
    import os

    secret = os.environ.get("JARVIS_TOKEN_SECRET", secrets.token_hex(32))
    return TokenValidator(issuer="jarvis-cluster", secret=secret)


async def main():
    import sys

    tv = build_jarvis_token_validator()
    await tv.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Token validator demo...")

        # Issue tokens
        access = tv.issue(
            "inference-gw",
            TokenType.ACCESS,
            ttl_s=3600,
            roles=["role-inference"],
            scopes=["infer:read", "infer:exec"],
        )
        refresh = tv.issue("inference-gw", TokenType.REFRESH, ttl_s=86400)
        api_key = tv.issue_api_key("admin-cli", scopes=["*"])

        for label, tok in [
            ("access", access),
            ("refresh", refresh),
            ("api_key", api_key),
        ]:
            r = tv.validate(tok)
            print(
                f"  {label:<10} → {r.status.value} sub={r.claims.sub if r.claims else '?'} ttl={r.claims.ttl:.0f}s"
            )

        # Scope/role check
        print(f"\n  has_scope infer:read  → {tv.has_scope(access, 'infer:read')}")
        print(f"  has_scope admin:*     → {tv.has_scope(access, 'admin:*')}")
        print(f"  has_role  role-inference → {tv.has_role(access, 'role-inference')}")

        # Revoke
        tv.revoke(access)
        r2 = tv.validate(access)
        print(f"\n  After revoke → {r2.status.value}")

        # Refresh
        new_tok = tv.refresh(refresh)
        r3 = tv.validate(new_tok) if new_tok else None
        print(f"  Refreshed   → {r3.status.value if r3 else 'failed'}")

        print(f"\nStats: {json.dumps(tv.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(tv.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
