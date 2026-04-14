#!/usr/bin/env python3
"""JARVIS Circuit Breaker — prevent cascade failures on LLM/service errors"""
import redis, json, time
from datetime import datetime, timedelta

r = redis.Redis(decode_responses=True)

THRESHOLD = 3       # failures before open
TIMEOUT = 60        # seconds before half-open retry
PREFIX = "jarvis:cb"

def _key(service: str) -> str:
    return f"{PREFIX}:{service}"

def get_state(service: str) -> str:
    """Returns: closed (ok), open (blocked), half_open (testing)"""
    data = r.hgetall(_key(service))
    if not data:
        return "closed"
    failures = int(data.get("failures", 0))
    last_fail = float(data.get("last_fail", 0))
    state = data.get("state", "closed")
    
    if state == "open" and time.time() - last_fail > TIMEOUT:
        r.hset(_key(service), "state", "half_open")
        return "half_open"
    return state

def record_success(service: str):
    state = get_state(service)
    if state in ("half_open", "open"):
        r.delete(_key(service))  # reset
    else:
        r.hincrby(_key(service), "successes", 1)

def record_failure(service: str):
    r.hincrby(_key(service), "failures", 1)
    r.hset(_key(service), "last_fail", time.time())
    failures = int(r.hget(_key(service), "failures") or 1)
    if failures >= THRESHOLD:
        r.hset(_key(service), "state", "open")
        r.publish("jarvis:events", json.dumps({
            "type": "circuit_open", "data": {"service": service, "failures": failures},
            "severity": "warning", "ts": datetime.now().isoformat()[:19]
        }))

def is_allowed(service: str) -> bool:
    state = get_state(service)
    return state != "open"

def status_all() -> dict:
    res = {}
    for k in r.scan_iter(f"{PREFIX}:*"):
        svc = k.replace(f"{PREFIX}:", "")
        res[svc] = {"state": get_state(svc), **r.hgetall(k)}
    return res

if __name__ == "__main__":
    for i in range(4):
        record_failure("m2_llm")
    print("State:", get_state("m2_llm"))
    print("Allowed:", is_allowed("m2_llm"))
    print("All:", status_all())
