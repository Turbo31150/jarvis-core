#!/usr/bin/env python3
"""JARVIS Audit Log — Immutable append-only audit trail for all system actions"""

import redis
import json
import time

r = redis.Redis(decode_responses=True)

STREAM_KEY = "jarvis:audit_log"
STATS_KEY = "jarvis:audit_log:stats"
INDEX_PREFIX = "jarvis:audit_log:idx:"

SEVERITY = {"debug": 0, "info": 1, "warn": 2, "error": 3, "critical": 4}


def log(
    action: str,
    actor: str,
    resource: str,
    outcome: str,
    details: dict = None,
    severity: str = "info",
) -> str:
    """Append an audit entry. Returns entry ID."""
    entry = {
        "action": action,
        "actor": actor,
        "resource": resource,
        "outcome": outcome,  # "success" | "failure" | "denied"
        "severity": severity,
        "details": json.dumps(details or {}),
        "ts": str(time.time()),
        "sev_level": str(SEVERITY.get(severity, 1)),
    }
    # Append to stream (immutable — no trim)
    eid = r.xadd(STREAM_KEY, entry, maxlen=10000, approximate=True)

    # Secondary indexes for fast lookup
    r.sadd(f"{INDEX_PREFIX}actor:{actor}", eid)
    r.expire(f"{INDEX_PREFIX}actor:{actor}", 86400 * 30)
    r.sadd(f"{INDEX_PREFIX}action:{action}", eid)
    r.expire(f"{INDEX_PREFIX}action:{action}", 86400 * 7)

    if outcome == "failure" or outcome == "denied":
        r.sadd(f"{INDEX_PREFIX}failures", eid)
        r.expire(f"{INDEX_PREFIX}failures", 86400 * 7)

    r.hincrby(STATS_KEY, f"action:{action}", 1)
    r.hincrby(STATS_KEY, f"actor:{actor}", 1)
    r.hincrby(STATS_KEY, f"outcome:{outcome}", 1)
    return eid


def query(
    actor: str = None,
    action: str = None,
    outcome: str = None,
    since_ts: float = None,
    limit: int = 50,
) -> list:
    """Query audit log with filters."""
    start = str(int((since_ts or (time.time() - 86400)) * 1000)) + "-0"
    raw = r.xrange(STREAM_KEY, start, "+", count=limit * 3)
    results = []
    for eid, fields in raw:
        if actor and fields.get("actor") != actor:
            continue
        if action and fields.get("action") != action:
            continue
        if outcome and fields.get("outcome") != outcome:
            continue
        results.append(
            {
                "id": eid,
                "action": fields["action"],
                "actor": fields["actor"],
                "resource": fields["resource"],
                "outcome": fields["outcome"],
                "severity": fields["severity"],
                "details": json.loads(fields.get("details", "{}")),
                "ts": float(fields["ts"]),
            }
        )
        if len(results) >= limit:
            break
    return results


def recent(limit: int = 20, min_severity: str = "info") -> list:
    min_level = SEVERITY.get(min_severity, 1)
    raw = r.xrevrange(STREAM_KEY, "+", "-", count=limit * 2)
    results = []
    for eid, fields in raw:
        if int(fields.get("sev_level", 1)) < min_level:
            continue
        results.append(
            {
                "id": eid,
                "action": fields["action"],
                "actor": fields["actor"],
                "outcome": fields["outcome"],
                "severity": fields["severity"],
                "ts": float(fields["ts"]),
            }
        )
        if len(results) >= limit:
            break
    return results


def stats() -> dict:
    s = r.hgetall(STATS_KEY)
    total = r.xlen(STREAM_KEY)
    actions = {
        k.replace("action:", ""): int(v)
        for k, v in s.items()
        if k.startswith("action:")
    }
    outcomes = {
        k.replace("outcome:", ""): int(v)
        for k, v in s.items()
        if k.startswith("outcome:")
    }
    return {"total_entries": total, "by_action": actions, "by_outcome": outcomes}


if __name__ == "__main__":
    events = [
        ("model_load", "scheduler", "m32:mistral", "success", {"vram": 6.0}, "info"),
        (
            "llm_request",
            "api_gateway",
            "m2:qwen35b",
            "success",
            {"tokens": 450},
            "info",
        ),
        (
            "llm_request",
            "api_gateway",
            "m2:qwen35b",
            "failure",
            {"error": "timeout"},
            "warn",
        ),
        (
            "config_change",
            "turbo",
            "rate_limiter",
            "success",
            {"key": "m2.rps"},
            "warn",
        ),
        (
            "model_unload",
            "governor",
            "ol1:gemma3",
            "success",
            {"freed_gb": 3.5},
            "info",
        ),
        (
            "auth_check",
            "unknown_host",
            "api:8767",
            "denied",
            {"ip": "10.0.0.9"},
            "error",
        ),
    ]
    for args in events:
        eid = log(*args)
        print(f"  [{args[5]:8s}] {args[0]:15s} by {args[1]:12s} → {args[3]}")

    print(
        f"\nFailures: {len(query(outcome='failure'))} | Denials: {len(query(outcome='denied'))}"
    )
    print(f"Stats: {stats()}")
