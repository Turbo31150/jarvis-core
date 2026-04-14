#!/usr/bin/env python3
"""JARVIS Policy Engine — Enforce access policies, rate limits, and content rules"""

import redis
import json
import time
import re
import hashlib

r = redis.Redis(decode_responses=True)

POLICIES_KEY = "jarvis:policy:registry"
VIOLATIONS_KEY = "jarvis:policy:violations"
STATS_KEY = "jarvis:policy:stats"

BUILTIN_POLICIES = [
    {
        "id": "max_prompt_length",
        "desc": "Reject prompts over 8000 chars",
        "type": "content",
        "rule": {"field": "prompt", "op": "len_gt", "value": 8000},
        "action": "reject",
        "severity": "warn",
    },
    {
        "id": "no_secrets_in_prompt",
        "desc": "Block prompts containing API keys or passwords",
        "type": "content",
        "rule": {
            "field": "prompt",
            "op": "regex",
            "value": r"(?i)(api[_-]?key|password|secret|token)\s*[:=]\s*\S{8,}",
        },
        "action": "redact",
        "severity": "error",
    },
    {
        "id": "rate_limit_per_identity",
        "desc": "Max 100 requests per minute per identity",
        "type": "rate",
        "rule": {"window_s": 60, "max_count": 100},
        "action": "throttle",
        "severity": "warn",
    },
    {
        "id": "block_known_bad_sources",
        "desc": "Block requests from flagged IP ranges",
        "type": "source",
        "rule": {"blocked_prefixes": ["10.99.", "192.168.99."]},
        "action": "block",
        "severity": "error",
    },
    {
        "id": "max_tokens_per_request",
        "desc": "Cap max_tokens at 4096",
        "type": "content",
        "rule": {"field": "max_tokens", "op": "gt", "value": 4096},
        "action": "clamp",
        "clamp_value": 4096,
        "severity": "info",
    },
]


def bootstrap():
    for p in BUILTIN_POLICIES:
        r.hset(POLICIES_KEY, p["id"], json.dumps(p))
    return len(BUILTIN_POLICIES)


def _check_rate(identity: str, policy: dict) -> bool:
    window = policy["rule"]["window_s"]
    max_count = policy["rule"]["max_count"]
    key = f"jarvis:policy:rate:{policy['id']}:{hashlib.md5(identity.encode()).hexdigest()[:8]}"
    count = r.incr(key)
    if count == 1:
        r.expire(key, window)
    return count <= max_count


def evaluate(request: dict) -> dict:
    """
    Evaluate a request against all policies.
    request: {prompt, identity, source_ip, max_tokens, ...}
    Returns: {allowed, violations, modified_request}
    """
    policies = [json.loads(v) for v in r.hvals(POLICIES_KEY)]
    violations = []
    allowed = True
    modified = dict(request)

    for policy in policies:
        ptype = policy["type"]
        rule = policy["rule"]
        action = policy["action"]

        triggered = False

        if ptype == "content":
            field = rule.get("field", "prompt")
            val = request.get(field, "")
            op = rule.get("op")
            if op == "len_gt" and len(str(val)) > rule["value"]:
                triggered = True
            elif op == "regex" and re.search(rule["value"], str(val)):
                triggered = True
            elif op == "gt" and isinstance(val, (int, float)) and val > rule["value"]:
                triggered = True

        elif ptype == "rate":
            identity = request.get("identity", "anonymous")
            if not _check_rate(identity, policy):
                triggered = True

        elif ptype == "source":
            src = request.get("source_ip", "")
            if any(src.startswith(p) for p in rule.get("blocked_prefixes", [])):
                triggered = True

        if triggered:
            violations.append(
                {
                    "policy": policy["id"],
                    "desc": policy["desc"],
                    "action": action,
                    "severity": policy["severity"],
                }
            )
            r.hincrby(STATS_KEY, f"policy:{policy['id']}", 1)
            r.lpush(
                VIOLATIONS_KEY,
                json.dumps(
                    {
                        "policy": policy["id"],
                        "request_hash": hashlib.md5(str(request).encode()).hexdigest()[
                            :8
                        ],
                        "ts": time.time(),
                    }
                ),
            )
            r.ltrim(VIOLATIONS_KEY, 0, 999)

            if action == "reject" or action == "block":
                allowed = False
            elif action == "clamp":
                field = rule.get("field", "max_tokens")
                modified[field] = policy.get("clamp_value", rule["value"])
            elif action == "redact":
                if "prompt" in modified:
                    modified["prompt"] = re.sub(
                        rule["value"], "[REDACTED]", modified["prompt"]
                    )

    r.hincrby(STATS_KEY, "evaluated", 1)
    if not allowed:
        r.hincrby(STATS_KEY, "blocked", 1)
    return {"allowed": allowed, "violations": violations, "request": modified}


def stats() -> dict:
    s = r.hgetall(STATS_KEY)
    return {k: int(v) for k, v in s.items()}


if __name__ == "__main__":
    bootstrap()
    tests = [
        {
            "prompt": "What is 2+2?",
            "identity": "user1",
            "source_ip": "192.168.1.10",
            "max_tokens": 500,
        },
        {
            "prompt": "x" * 9000,
            "identity": "user2",
            "source_ip": "192.168.1.20",
            "max_tokens": 100,
        },
        {
            "prompt": "Use api_key=sk-abc123xyz for auth",
            "identity": "user3",
            "source_ip": "192.168.1.30",
        },
        {"prompt": "Hello", "identity": "user4", "source_ip": "10.99.0.5"},
        {"prompt": "Generate text", "identity": "user5", "max_tokens": 8000},
    ]
    for req in tests:
        result = evaluate(req)
        icon = "✅" if result["allowed"] else "❌"
        viols = [(v["policy"], v["action"]) for v in result["violations"]]
        print(f"  {icon} {req['prompt'][:40]:40s} → {viols or 'ok'}")
    print(f"\nStats: {stats()}")
