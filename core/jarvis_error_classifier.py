#!/usr/bin/env python3
"""
jarvis_error_classifier — Classifies errors by type, severity, and recoverability
Maps exceptions and HTTP errors to actionable categories with retry guidance
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.error_classifier")

REDIS_PREFIX = "jarvis:errclass:"


class ErrorCategory(str, Enum):
    NETWORK = "network"  # connection, DNS, timeout
    RATE_LIMIT = "rate_limit"  # 429, quota exceeded
    AUTH = "auth"  # 401, 403, invalid key
    NOT_FOUND = "not_found"  # 404, model not found
    SERVER = "server"  # 5xx, internal errors
    CLIENT = "client"  # 400, bad request, invalid params
    TIMEOUT = "timeout"  # request/read timeout
    CONTEXT = "context"  # context length exceeded
    CONTENT = "content"  # content policy, moderation
    RESOURCE = "resource"  # OOM, VRAM, disk full
    UNKNOWN = "unknown"


class ErrorSeverity(str, Enum):
    LOW = "low"  # log and ignore
    MEDIUM = "medium"  # retry with backoff
    HIGH = "high"  # alert, failover
    CRITICAL = "critical"  # immediate escalation


class RecoveryAction(str, Enum):
    RETRY_IMMEDIATE = "retry_immediate"
    RETRY_BACKOFF = "retry_backoff"
    RETRY_AFTER = "retry_after"  # respect Retry-After header
    FAILOVER = "failover"  # switch to different backend
    REDUCE_CONTEXT = "reduce_context"
    CHANGE_MODEL = "change_model"
    FIX_REQUEST = "fix_request"  # bad params — fix before retry
    ESCALATE = "escalate"
    IGNORE = "ignore"


@dataclass
class ClassifiedError:
    category: ErrorCategory
    severity: ErrorSeverity
    recovery: RecoveryAction
    retryable: bool
    message: str
    original: str
    http_status: int = 0
    retry_after_s: float = 0.0
    context: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "category": self.category.value,
            "severity": self.severity.value,
            "recovery": self.recovery.value,
            "retryable": self.retryable,
            "message": self.message,
            "http_status": self.http_status,
            "retry_after_s": self.retry_after_s,
        }


# --- Pattern rules (ordered, first match wins) ---

_HTTP_RULES: list[tuple[int, ErrorCategory, ErrorSeverity, RecoveryAction]] = [
    (429, ErrorCategory.RATE_LIMIT, ErrorSeverity.MEDIUM, RecoveryAction.RETRY_AFTER),
    (401, ErrorCategory.AUTH, ErrorSeverity.HIGH, RecoveryAction.ESCALATE),
    (403, ErrorCategory.AUTH, ErrorSeverity.HIGH, RecoveryAction.ESCALATE),
    (404, ErrorCategory.NOT_FOUND, ErrorSeverity.MEDIUM, RecoveryAction.CHANGE_MODEL),
    (400, ErrorCategory.CLIENT, ErrorSeverity.MEDIUM, RecoveryAction.FIX_REQUEST),
    (413, ErrorCategory.CONTEXT, ErrorSeverity.MEDIUM, RecoveryAction.REDUCE_CONTEXT),
    (422, ErrorCategory.CLIENT, ErrorSeverity.MEDIUM, RecoveryAction.FIX_REQUEST),
    (500, ErrorCategory.SERVER, ErrorSeverity.HIGH, RecoveryAction.RETRY_BACKOFF),
    (502, ErrorCategory.SERVER, ErrorSeverity.HIGH, RecoveryAction.FAILOVER),
    (503, ErrorCategory.SERVER, ErrorSeverity.HIGH, RecoveryAction.RETRY_BACKOFF),
    (504, ErrorCategory.TIMEOUT, ErrorSeverity.HIGH, RecoveryAction.RETRY_BACKOFF),
]

_MSG_PATTERNS: list[tuple[re.Pattern, ErrorCategory, ErrorSeverity, RecoveryAction]] = [
    (
        re.compile(r"context.{0,20}(length|window|limit|exceed)", re.I),
        ErrorCategory.CONTEXT,
        ErrorSeverity.MEDIUM,
        RecoveryAction.REDUCE_CONTEXT,
    ),
    (
        re.compile(r"(rate.?limit|quota|too many req)", re.I),
        ErrorCategory.RATE_LIMIT,
        ErrorSeverity.MEDIUM,
        RecoveryAction.RETRY_AFTER,
    ),
    (
        re.compile(r"(timeout|timed.?out|deadline)", re.I),
        ErrorCategory.TIMEOUT,
        ErrorSeverity.MEDIUM,
        RecoveryAction.RETRY_BACKOFF,
    ),
    (
        re.compile(
            r"(connection.?(refused|reset|error)|network|dns|unreachable)", re.I
        ),
        ErrorCategory.NETWORK,
        ErrorSeverity.HIGH,
        RecoveryAction.FAILOVER,
    ),
    (
        re.compile(r"(unauthorized|invalid.?key|api.?key|authentication)", re.I),
        ErrorCategory.AUTH,
        ErrorSeverity.HIGH,
        RecoveryAction.ESCALATE,
    ),
    (
        re.compile(r"(out.?of.?memory|oom|vram|cuda.?error|memory.?alloc)", re.I),
        ErrorCategory.RESOURCE,
        ErrorSeverity.CRITICAL,
        RecoveryAction.FAILOVER,
    ),
    (
        re.compile(r"(content.?policy|moderation|safety|harmful)", re.I),
        ErrorCategory.CONTENT,
        ErrorSeverity.MEDIUM,
        RecoveryAction.FIX_REQUEST,
    ),
    (
        re.compile(r"(model.?(not.?found|unavailable|loading))", re.I),
        ErrorCategory.NOT_FOUND,
        ErrorSeverity.HIGH,
        RecoveryAction.CHANGE_MODEL,
    ),
    (
        re.compile(r"(500|internal.?server.?error)", re.I),
        ErrorCategory.SERVER,
        ErrorSeverity.HIGH,
        RecoveryAction.RETRY_BACKOFF,
    ),
]

_RETRYABLE = {
    ErrorCategory.NETWORK,
    ErrorCategory.RATE_LIMIT,
    ErrorCategory.SERVER,
    ErrorCategory.TIMEOUT,
    ErrorCategory.RESOURCE,
}

_NOT_RETRYABLE = {
    ErrorCategory.AUTH,
    ErrorCategory.CONTENT,
    ErrorCategory.CLIENT,
}


class ErrorClassifier:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._history: list[ClassifiedError] = []
        self._counts: dict[str, int] = {}
        self._stats: dict[str, int] = {"classified": 0, "retryable": 0, "critical": 0}

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def classify_http(
        self,
        status: int,
        body: str = "",
        headers: dict | None = None,
    ) -> ClassifiedError:
        # Match HTTP status first
        cat, sev, rec = (
            ErrorCategory.UNKNOWN,
            ErrorSeverity.MEDIUM,
            RecoveryAction.RETRY_BACKOFF,
        )
        for code, c, s, r in _HTTP_RULES:
            if status == code:
                cat, sev, rec = c, s, r
                break

        # Refine from body if available
        if body:
            for pattern, pc, ps, pr in _MSG_PATTERNS:
                if pattern.search(body):
                    cat, sev, rec = pc, ps, pr
                    break

        retry_after = 0.0
        if headers and "retry-after" in {k.lower(): v for k, v in headers.items()}:
            h = {k.lower(): v for k, v in headers.items()}
            try:
                retry_after = float(h.get("retry-after", 0))
            except ValueError:
                retry_after = 60.0

        retryable = cat in _RETRYABLE and cat not in _NOT_RETRYABLE
        msg = f"HTTP {status}: {body[:100]}" if body else f"HTTP {status}"

        err = ClassifiedError(
            category=cat,
            severity=sev,
            recovery=rec,
            retryable=retryable,
            message=msg,
            original=body[:200],
            http_status=status,
            retry_after_s=retry_after,
        )
        self._record(err)
        return err

    def classify_exception(self, exc: Exception) -> ClassifiedError:
        msg = str(exc)
        exc_type = type(exc).__name__

        cat, sev, rec = (
            ErrorCategory.UNKNOWN,
            ErrorSeverity.MEDIUM,
            RecoveryAction.RETRY_BACKOFF,
        )

        # Exception type hints
        if "Timeout" in exc_type or "TimeoutError" in exc_type:
            cat, sev, rec = (
                ErrorCategory.TIMEOUT,
                ErrorSeverity.MEDIUM,
                RecoveryAction.RETRY_BACKOFF,
            )
        elif "Connection" in exc_type or "Network" in exc_type:
            cat, sev, rec = (
                ErrorCategory.NETWORK,
                ErrorSeverity.HIGH,
                RecoveryAction.FAILOVER,
            )
        elif "Memory" in exc_type or "OOM" in exc_type:
            cat, sev, rec = (
                ErrorCategory.RESOURCE,
                ErrorSeverity.CRITICAL,
                RecoveryAction.FAILOVER,
            )
        else:
            # Scan message patterns
            for pattern, pc, ps, pr in _MSG_PATTERNS:
                if pattern.search(msg):
                    cat, sev, rec = pc, ps, pr
                    break

        retryable = cat in _RETRYABLE
        err = ClassifiedError(
            category=cat,
            severity=sev,
            recovery=rec,
            retryable=retryable,
            message=f"{exc_type}: {msg[:120]}",
            original=msg[:200],
            context={"exc_type": exc_type},
        )
        self._record(err)
        return err

    def classify_text(self, text: str) -> ClassifiedError:
        cat, sev, rec = ErrorCategory.UNKNOWN, ErrorSeverity.LOW, RecoveryAction.IGNORE
        for pattern, pc, ps, pr in _MSG_PATTERNS:
            if pattern.search(text):
                cat, sev, rec = pc, ps, pr
                break
        retryable = cat in _RETRYABLE
        err = ClassifiedError(
            category=cat,
            severity=sev,
            recovery=rec,
            retryable=retryable,
            message=text[:120],
            original=text[:200],
        )
        self._record(err)
        return err

    def _record(self, err: ClassifiedError):
        self._stats["classified"] += 1
        if err.retryable:
            self._stats["retryable"] += 1
        if err.severity == ErrorSeverity.CRITICAL:
            self._stats["critical"] += 1
        self._counts[err.category.value] = self._counts.get(err.category.value, 0) + 1
        self._history.append(err)
        if len(self._history) > 200:
            self._history.pop(0)

    def recent(self, n: int = 10, category: ErrorCategory | None = None) -> list[dict]:
        h = self._history
        if category:
            h = [e for e in h if e.category == category]
        return [e.to_dict() for e in h[-n:]]

    def stats(self) -> dict:
        return {**self._stats, "by_category": self._counts}


def build_jarvis_error_classifier() -> ErrorClassifier:
    return ErrorClassifier()


async def main():
    import sys

    clf = build_jarvis_error_classifier()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        cases = [
            (429, '{"error":"rate limit exceeded"}', {"Retry-After": "30"}),
            (500, '{"error":"internal server error"}', {}),
            (401, '{"error":"invalid api key"}', {}),
            (400, '{"error":"context length exceeded 4096"}', {}),
            (503, "", {}),
        ]
        print("HTTP classification:")
        for status, body, headers in cases:
            r = clf.classify_http(status, body, headers)
            print(
                f"  {status} → {r.category.value:<15} {r.severity.value:<10} "
                f"retry={r.retryable} action={r.recovery.value}"
            )

        print("\nException classification:")
        excs = [
            TimeoutError("request timed out after 10s"),
            ConnectionRefusedError("Connection refused"),
            RuntimeError("CUDA out of memory"),
            ValueError("model not found: qwen3-999b"),
        ]
        for exc in excs:
            r = clf.classify_exception(exc)
            print(
                f"  {type(exc).__name__:<25} → {r.category.value:<15} {r.recovery.value}"
            )

        print(f"\nStats: {json.dumps(clf.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(clf.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
