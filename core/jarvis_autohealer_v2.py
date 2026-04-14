#!/usr/bin/env python3
"""JARVIS Auto Healer v2 — Detect and repair common failure modes automatically"""

import redis
import json
import time
import subprocess
import requests

r = redis.Redis(decode_responses=True)

HEAL_LOG_KEY = "jarvis:healer_v2:log"
STATS_KEY = "jarvis:healer_v2:stats"
COOLDOWN_PREFIX = "jarvis:healer_v2:cooldown:"

HEAL_RULES = [
    {
        "id": "reset_open_circuit_m2",
        "desc": "Reset M2 circuit breaker if backend is now reachable",
        "check": lambda: r.hget("jarvis:cb:m2", "state") == "open",
        "verify": lambda: _is_reachable("http://192.168.1.26:1234/v1/models", 3),
        "heal": lambda: _reset_circuit("m2"),
        "cooldown_s": 120,
    },
    {
        "id": "reset_open_circuit_ol1",
        "desc": "Reset OL1 circuit breaker if backend is now reachable",
        "check": lambda: r.hget("jarvis:cb:ol1", "state") == "open",
        "verify": lambda: _is_reachable("http://127.0.0.1:11434/api/tags", 3),
        "heal": lambda: _reset_circuit("ol1"),
        "cooldown_s": 60,
    },
    {
        "id": "clear_stale_rate_limits",
        "desc": "Clear rate limit overrides older than 10 min",
        "check": lambda: bool(r.exists("jarvis:rate_opt:m2:rps_override")),
        "verify": lambda: (
            (
                time.time()
                - float(r.object("idletime", "jarvis:rate_opt:m2:rps_override") or 0)
            )
            > 600
            if r.exists("jarvis:rate_opt:m2:rps_override")
            else False
        ),
        "heal": lambda: r.delete("jarvis:rate_opt:m2:rps_override"),
        "cooldown_s": 300,
    },
    {
        "id": "restart_api_gateway",
        "desc": "Restart API gateway if health check fails",
        "check": lambda: not _is_reachable("http://127.0.0.1:8767/health", 2),
        "verify": lambda: True,
        "heal": lambda: _restart_service("jarvis-api"),
        "cooldown_s": 300,
    },
    {
        "id": "restart_whisper_api",
        "desc": "Restart whisper API if health check fails",
        "check": lambda: not _is_reachable("http://127.0.0.1:9743/health", 2),
        "verify": lambda: True,
        "heal": lambda: _restart_service("jarvis-whisper-api"),
        "cooldown_s": 180,
    },
]


def _is_reachable(url: str, timeout: int = 3) -> bool:
    try:
        resp = requests.get(url, timeout=timeout)
        return resp.status_code == 200
    except Exception:
        return False


def _reset_circuit(backend: str) -> bool:
    r.hset(f"jarvis:cb:{backend}", "state", "half_open")
    r.hset(f"jarvis:cb:{backend}", "failures", "0")
    return True


def _restart_service(service: str) -> bool:
    try:
        result = subprocess.run(
            ["sudo", "systemctl", "restart", service],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.returncode == 0
    except Exception:
        return False


def _is_on_cooldown(rule_id: str) -> bool:
    return r.exists(f"{COOLDOWN_PREFIX}{rule_id}") > 0


def _set_cooldown(rule_id: str, seconds: int):
    r.setex(f"{COOLDOWN_PREFIX}{rule_id}", seconds, "1")


def run_cycle() -> dict:
    """Run one healing cycle."""
    healed = []
    checked = []

    for rule in HEAL_RULES:
        rule_id = rule["id"]
        if _is_on_cooldown(rule_id):
            continue

        try:
            needs_heal = rule["check"]()
        except Exception:
            needs_heal = False

        if not needs_heal:
            continue

        checked.append(rule_id)

        try:
            verified = rule["verify"]()
        except Exception:
            verified = True  # proceed if verify fails

        if not verified:
            continue

        try:
            t0 = time.perf_counter()
            success = rule["heal"]()
            lat = round((time.perf_counter() - t0) * 1000)
            _set_cooldown(rule_id, rule["cooldown_s"])

            entry = {
                "rule": rule_id,
                "desc": rule["desc"],
                "success": success,
                "latency_ms": lat,
                "ts": time.time(),
            }
            healed.append(entry)
            r.lpush(HEAL_LOG_KEY, json.dumps(entry))
            r.ltrim(HEAL_LOG_KEY, 0, 99)
            r.hincrby(STATS_KEY, f"{rule_id}:heals", 1)
        except Exception as e:
            healed.append({"rule": rule_id, "success": False, "error": str(e)[:60]})

    r.hincrby(STATS_KEY, "cycles", 1)
    return {"checked": checked, "healed": healed, "ts": time.time()}


def recent_heals(limit: int = 10) -> list:
    raw = r.lrange(HEAL_LOG_KEY, 0, limit - 1)
    return [json.loads(x) for x in raw]


def stats() -> dict:
    s = r.hgetall(STATS_KEY)
    return {
        "rules": len(HEAL_RULES),
        **{k: int(v) for k, v in s.items()},
        "recent": recent_heals(3),
    }


if __name__ == "__main__":
    # Simulate M2 circuit open
    r.hset("jarvis:cb:m2", "state", "open")
    r.hset("jarvis:cb:m2", "failures", "5")

    result = run_cycle()
    print(
        f"Heal cycle: {len(result['checked'])} checked, {len(result['healed'])} healed"
    )
    for h in result["healed"]:
        icon = "✅" if h["success"] else "❌"
        print(f"  {icon} [{h['rule']}] {h.get('desc', '')}")

    # Check M2 state after heal
    new_state = r.hget("jarvis:cb:m2", "state")
    print(f"\nM2 circuit state after heal: {new_state}")
    print(f"Stats: {stats()}")
