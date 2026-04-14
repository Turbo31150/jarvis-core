#!/usr/bin/env python3
"""JARVIS Request Logger — Structured logging of all API requests"""

import redis
import json
import time
import uuid
from datetime import datetime

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:reqlog"
MAX_LOGS = 10000


def log_request(
    method: str,
    path: str,
    status_code: int,
    latency_ms: int,
    user_id: str = "anonymous",
    body_size: int = 0,
    ip: str = "127.0.0.1",
    request_id: str = None,
) -> str:
    rid = request_id or uuid.uuid4().hex[:8]
    entry = {
        "id": rid,
        "ts": datetime.now().isoformat()[:23],
        "method": method,
        "path": path,
        "status": status_code,
        "ms": latency_ms,
        "user": user_id,
        "size": body_size,
        "ip": ip,
    }
    r.lpush(f"{PREFIX}:all", json.dumps(entry))
    r.ltrim(f"{PREFIX}:all", 0, MAX_LOGS - 1)

    # Stats
    today = datetime.now().strftime("%Y-%m-%d")
    r.hincrby(f"{PREFIX}:stats:{today}", "total", 1)
    r.hincrby(f"{PREFIX}:stats:{today}", f"status_{status_code // 100}xx", 1)
    r.expire(f"{PREFIX}:stats:{today}", 86400 * 30)

    # Slow request alert
    if latency_ms > 5000:
        r.lpush(f"{PREFIX}:slow", json.dumps(entry))
        r.ltrim(f"{PREFIX}:slow", 0, 99)

    # Error log
    if status_code >= 400:
        r.lpush(f"{PREFIX}:errors", json.dumps(entry))
        r.ltrim(f"{PREFIX}:errors", 0, 499)

    # Path stats
    r.hincrby(f"{PREFIX}:paths:{path}", "count", 1)
    r.hincrby(f"{PREFIX}:paths:{path}", "total_ms", latency_ms)
    return rid


def recent(n: int = 20) -> list:
    raw = r.lrange(f"{PREFIX}:all", 0, n - 1)
    return [json.loads(e) for e in raw]


def slow_requests(n: int = 10) -> list:
    raw = r.lrange(f"{PREFIX}:slow", 0, n - 1)
    return [json.loads(e) for e in raw]


def error_requests(n: int = 20) -> list:
    raw = r.lrange(f"{PREFIX}:errors", 0, n - 1)
    return [json.loads(e) for e in raw]


def top_paths(n: int = 10) -> list:
    result = []
    for key in r.scan_iter(f"{PREFIX}:paths:*"):
        path = key.replace(f"{PREFIX}:paths:", "")
        data = r.hgetall(key)
        count = int(data.get("count", 0))
        avg_ms = int(data.get("total_ms", 0)) // max(count, 1)
        result.append({"path": path, "count": count, "avg_ms": avg_ms})
    return sorted(result, key=lambda x: -x["count"])[:n]


def stats() -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    data = r.hgetall(f"{PREFIX}:stats:{today}")
    total = int(data.get("total", 0))
    errors = int(data.get("status_4xx", 0)) + int(data.get("status_5xx", 0))
    return {
        "today": total,
        "errors": errors,
        "error_rate_pct": round(errors / max(total, 1) * 100, 2),
        "status": {k: int(v) for k, v in data.items()},
        "top_paths": top_paths(5),
    }


if __name__ == "__main__":
    # Simulate requests
    for path, code, ms in [
        ("/health", 200, 1), ("/score", 200, 2), ("/llm/ask", 200, 850),
        ("/health", 200, 1), ("/sla", 200, 3), ("/health", 500, 15),
        ("/health", 200, 1), ("/mesh", 200, 5), ("/unknown", 404, 2),
    ]:
        log_request("GET", path, code, ms)

    s = stats()
    print(f"Today: {s['today']} requests, {s['error_rate_pct']}% errors")
    print("Top paths:")
    for p in s["top_paths"]:
        print(f"  {p['path']}: {p['count']} reqs, avg {p['avg_ms']}ms")
