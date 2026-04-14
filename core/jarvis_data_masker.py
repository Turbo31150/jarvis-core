#!/usr/bin/env python3
"""
jarvis_data_masker — PII and sensitive data masking for logs and LLM outputs
Regex-based detection with configurable masking strategies and audit trail
"""

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.data_masker")

AUDIT_FILE = Path("/home/turbo/IA/Core/jarvis/data/masking_audit.jsonl")
REDIS_PREFIX = "jarvis:masker:"


class MaskStrategy(str, Enum):
    REDACT = "redact"  # replace with [REDACTED]
    HASH = "hash"  # replace with SHA256 prefix
    PARTIAL = "partial"  # show first/last N chars
    TOKENIZE = "tokenize"  # replace with stable token (deterministic)
    BLANK = "blank"  # replace with ***


class DataCategory(str, Enum):
    EMAIL = "email"
    PHONE = "phone"
    IP_ADDRESS = "ip_address"
    API_KEY = "api_key"
    JWT = "jwt"
    CREDIT_CARD = "credit_card"
    SSN = "ssn"
    PASSWORD = "password"
    URL_WITH_CREDS = "url_with_creds"
    AWS_KEY = "aws_key"
    PRIVATE_KEY = "private_key"
    BITCOIN_ADDR = "bitcoin_addr"
    IBAN = "iban"
    CUSTOM = "custom"


@dataclass
class MaskPattern:
    category: DataCategory
    pattern: re.Pattern
    strategy: MaskStrategy = MaskStrategy.REDACT
    partial_keep: int = 4  # for PARTIAL strategy, keep N chars at start/end
    enabled: bool = True
    description: str = ""


@dataclass
class MaskResult:
    original_length: int
    masked_text: str
    detections: list[dict] = field(default_factory=list)
    categories_found: list[str] = field(default_factory=list)
    mask_count: int = 0

    @property
    def was_modified(self) -> bool:
        return self.mask_count > 0

    def to_dict(self) -> dict:
        return {
            "was_modified": self.was_modified,
            "mask_count": self.mask_count,
            "categories": sorted(set(self.categories_found)),
            "masked_length": len(self.masked_text),
        }


def _apply_strategy(
    match_text: str, strategy: MaskStrategy, partial_keep: int, category: str
) -> str:
    if strategy == MaskStrategy.REDACT:
        return f"[{category.upper()}]"
    elif strategy == MaskStrategy.BLANK:
        return "*" * min(len(match_text), 8)
    elif strategy == MaskStrategy.HASH:
        h = hashlib.sha256(match_text.encode()).hexdigest()[:8]
        return f"[{category.upper()}:{h}]"
    elif strategy == MaskStrategy.TOKENIZE:
        h = hashlib.md5(match_text.encode()).hexdigest()[:6].upper()
        return f"<{category.upper()}-{h}>"
    elif strategy == MaskStrategy.PARTIAL:
        n = partial_keep
        if len(match_text) <= n * 2:
            return "*" * len(match_text)
        return match_text[:n] + "***" + match_text[-n:]
    return f"[{category.upper()}]"


# Built-in patterns
_BUILTIN_PATTERNS: list[tuple[DataCategory, str, MaskStrategy, str]] = [
    (
        DataCategory.EMAIL,
        r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b",
        MaskStrategy.PARTIAL,
        "Email address",
    ),
    (
        DataCategory.PHONE,
        r"(?<!\d)(?:\+?\d{1,3}[\s\-.]?)?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}(?!\d)",
        MaskStrategy.REDACT,
        "Phone number",
    ),
    (
        DataCategory.IP_ADDRESS,
        r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b",
        MaskStrategy.PARTIAL,
        "IPv4 address",
    ),
    (
        DataCategory.JWT,
        r"eyJ[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+",
        MaskStrategy.HASH,
        "JWT token",
    ),
    (
        DataCategory.API_KEY,
        r"(?i)(?:api[_\-]?key|token|secret|apikey)\s*[=:]\s*['\"]?([a-zA-Z0-9\-_]{20,})['\"]?",
        MaskStrategy.REDACT,
        "API key / secret",
    ),
    (
        DataCategory.AWS_KEY,
        r"\b(?:AKIA|ABIA|ACCA|ASIA)[A-Z0-9]{16}\b",
        MaskStrategy.REDACT,
        "AWS access key",
    ),
    (
        DataCategory.CREDIT_CARD,
        r"\b(?:4\d{12}(?:\d{3})?|5[1-5]\d{14}|3[47]\d{13}|6011\d{12})\b",
        MaskStrategy.PARTIAL,
        "Credit card number",
    ),
    (
        DataCategory.SSN,
        r"\b\d{3}[-\s]?\d{2}[-\s]?\d{4}\b",
        MaskStrategy.REDACT,
        "Social Security Number",
    ),
    (
        DataCategory.URL_WITH_CREDS,
        r"(?:https?|ftp)://[^:@\s]+:[^@\s]+@[^\s]+",
        MaskStrategy.REDACT,
        "URL with credentials",
    ),
    (
        DataCategory.PASSWORD,
        r"(?i)(?:password|passwd|pwd)\s*[=:]\s*['\"]?(\S{4,})['\"]?",
        MaskStrategy.REDACT,
        "Password field",
    ),
    (
        DataCategory.PRIVATE_KEY,
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----",
        MaskStrategy.REDACT,
        "Private key header",
    ),
    (
        DataCategory.BITCOIN_ADDR,
        r"\b[13][a-km-zA-HJ-NP-Z1-9]{25,34}\b",
        MaskStrategy.HASH,
        "Bitcoin address",
    ),
]


class DataMasker:
    def __init__(self, audit: bool = True):
        self.redis: aioredis.Redis | None = None
        self._patterns: list[MaskPattern] = []
        self._audit = audit
        self._stats: dict[str, int] = {
            "texts_processed": 0,
            "texts_modified": 0,
            "total_detections": 0,
        }
        self._category_counts: dict[str, int] = {}
        if audit:
            AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._load_builtins()

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def _load_builtins(self):
        for cat, pattern, strategy, desc in _BUILTIN_PATTERNS:
            self._patterns.append(
                MaskPattern(
                    category=cat,
                    pattern=re.compile(pattern),
                    strategy=strategy,
                    description=desc,
                )
            )

    def add_pattern(
        self,
        category: DataCategory,
        pattern: str,
        strategy: MaskStrategy = MaskStrategy.REDACT,
        description: str = "",
        partial_keep: int = 4,
    ):
        self._patterns.append(
            MaskPattern(
                category=category,
                pattern=re.compile(pattern),
                strategy=strategy,
                partial_keep=partial_keep,
                description=description,
            )
        )

    def disable(self, category: DataCategory):
        for p in self._patterns:
            if p.category == category:
                p.enabled = False

    def mask(
        self, text: str, categories: list[DataCategory] | None = None
    ) -> MaskResult:
        self._stats["texts_processed"] += 1
        detections: list[dict] = []
        categories_found: list[str] = []
        mask_count = 0
        result_text = text

        active = [
            p
            for p in self._patterns
            if p.enabled and (categories is None or p.category in categories)
        ]

        for mp in active:

            def replacer(m: re.Match) -> str:
                nonlocal mask_count
                full = m.group(0)
                replacement = _apply_strategy(
                    full, mp.strategy, mp.partial_keep, mp.category.value
                )
                detections.append(
                    {
                        "category": mp.category.value,
                        "original_len": len(full),
                        "strategy": mp.strategy.value,
                        "position": m.start(),
                    }
                )
                categories_found.append(mp.category.value)
                self._category_counts[mp.category.value] = (
                    self._category_counts.get(mp.category.value, 0) + 1
                )
                mask_count += 1
                return replacement

            result_text = mp.pattern.sub(replacer, result_text)

        result = MaskResult(
            original_length=len(text),
            masked_text=result_text,
            detections=detections,
            categories_found=categories_found,
            mask_count=mask_count,
        )

        self._stats["total_detections"] += mask_count
        if result.was_modified:
            self._stats["texts_modified"] += 1
            if self._audit:
                self._write_audit(result)

        return result

    def mask_messages(self, messages: list[dict]) -> list[dict]:
        """Mask content in a list of chat messages."""
        masked = []
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                r = self.mask(content)
                new_msg = dict(msg)
                new_msg["content"] = r.masked_text
                masked.append(new_msg)
            else:
                masked.append(msg)
        return masked

    def scan(self, text: str) -> list[str]:
        """Return list of detected categories without masking."""
        found = []
        for mp in self._patterns:
            if mp.enabled and mp.pattern.search(text):
                found.append(mp.category.value)
        return found

    def _write_audit(self, result: MaskResult):
        try:
            with open(AUDIT_FILE, "a") as f:
                entry = {
                    "ts": time.time(),
                    "categories": result.categories_found,
                    "mask_count": result.mask_count,
                }
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    def stats(self) -> dict:
        return {
            **self._stats,
            "patterns": len(self._patterns),
            "category_counts": self._category_counts,
        }


def build_jarvis_data_masker() -> DataMasker:
    return DataMasker(audit=True)


async def main():
    import sys

    masker = build_jarvis_data_masker()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        samples = [
            "Contact me at john.doe@example.com or call +1-555-867-5309",
            "API_KEY=sk-abc123XYZ789abcdefghijklmnopqrst",
            "My credit card is 4111111111111111 and SSN is 123-45-6789",
            "Connect to postgres://admin:s3cr3t@192.168.1.1:5432/db",
            "JWT: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ1c2VyMSJ9.abc123",
            "This text has no PII at all, just normal content.",
        ]

        print("Masking samples:")
        for text in samples:
            result = masker.mask(text)
            modified = "🔒" if result.was_modified else "✅"
            print(f"\n  {modified} Input:  {text[:80]}")
            if result.was_modified:
                print(f"     Output: {result.masked_text[:80]}")
                print(f"     Found:  {result.categories_found}")

        print(f"\nStats: {json.dumps(masker.stats(), indent=2)}")

    elif cmd == "scan":
        text = sys.argv[2] if len(sys.argv) > 2 else ""
        cats = masker.scan(text)
        print(f"Detected: {cats}")

    elif cmd == "stats":
        print(json.dumps(masker.stats(), indent=2))


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
