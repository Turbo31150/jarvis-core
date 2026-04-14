#!/usr/bin/env python3
"""JARVIS Alert Manager — Rule-based alerting with dedup, escalation and notification routing"""

import redis
import json
import time
import hashlib

r = redis.Redis(decode_responses=True)

RULE_PREFIX = "jarvis:alert:rule:"
ALERT_PREFIX = "jarvis:alert:active:"
HISTORY_KEY = "jarvis:alert:history"
STATS_KEY = "jarvis:alert:stats"
INDEX_KEY = "jarvis:alert:rules"

SEVERITIES = ["info", "warn", "error", "critical"]


def register_rule(
    name: str,
    condition_key: str,
    condition_op: str,
    threshold: float,
    severity: str = "warn",
    cooldown_s: int = 300,
    channels: list = None,
):
    rid = hashlib.md5(name.encode()).hexdigest()[:10]
    rule = {
        "id": rid,
        "name": name,
        "condition_key": condition_key,
        "condition_op": condition_op,  # gt, lt, eq, ne
        "threshold": threshold,
        "severity": severity,
        "cooldown_s": cooldown_s,
        "channels": channels or ["redis"],
        "enabled": True,
        "created_at": time.time(),
    }
    r.hset(RULE_PREFIX + rid, "config", json.dumps(rule))
    r.sadd(INDEX_KEY, rid)
    r.hincrby(STATS_KEY, "rules_registered", 1)
    return rid


def _evaluate(op: str, value: float, threshold: float) -> bool:
    ops = {
        "gt": value > threshold,
        "lt": value < threshold,
        "ge": value >= threshold,
        "le": value <= threshold,
        "eq": value == threshold,
        "ne": value != threshold,
    }
    return ops.get(op, False)


def _on_cooldown(rule_id: str) -> bool:
    return r.exists(f"jarvis:alert:cd:{rule_id}") > 0


def _set_cooldown(rule_id: str, seconds: int):
    r.setex(f"jarvis:alert:cd:{rule_id}", seconds, "1")


def evaluate_rules(metrics: dict) -> list:
    """Evaluate all rules against provided metrics dict. Return fired alerts."""
    fired = []
    rids = r.smembers(INDEX_KEY)

    for rid in rids:
        raw = r.hget(RULE_PREFIX + rid, "config")
        if not raw:
            continue
        rule = json.loads(raw)
        if not rule.get("enabled"):
            continue
        if _on_cooldown(rid):
            continue

        key = rule["condition_key"]
        value = metrics.get(key)
        if value is None:
            continue

        try:
            value = float(value)
        except (TypeError, ValueError):
            continue

        if _evaluate(rule["condition_op"], value, rule["threshold"]):
            alert = _fire_alert(rule, value)
            fired.append(alert)

    return fired


def _fire_alert(rule: dict, value: float) -> dict:
    alert_id = hashlib.md5(f"{rule['id']}:{time.time()}".encode()).hexdigest()[:10]
    alert = {
        "id": alert_id,
        "rule": rule["name"],
        "severity": rule["severity"],
        "value": value,
        "threshold": rule["threshold"],
        "message": f"{rule['name']}: {value} {rule['condition_op']} {rule['threshold']}",
        "channels": rule["channels"],
        "ts": time.time(),
        "acknowledged": False,
    }
    r.setex(f"{ALERT_PREFIX}{alert_id}", 3600, json.dumps(alert))
    r.lpush(HISTORY_KEY, json.dumps(alert))
    r.ltrim(HISTORY_KEY, 0, 999)
    _set_cooldown(rule["id"], rule["cooldown_s"])

    sev_idx = (
        SEVERITIES.index(rule["severity"]) if rule["severity"] in SEVERITIES else 1
    )
    r.zadd("jarvis:alert:active_set", {alert_id: sev_idx})
    r.hincrby(STATS_KEY, f"fired:{rule['severity']}", 1)
    r.hincrby(STATS_KEY, "total_fired", 1)
    return alert


def acknowledge(alert_id: str, by: str = "operator"):
    raw = r.get(f"{ALERT_PREFIX}{alert_id}")
    if raw:
        alert = json.loads(raw)
        alert["acknowledged"] = True
        alert["acked_by"] = by
        alert["acked_at"] = time.time()
        r.setex(f"{ALERT_PREFIX}{alert_id}", 3600, json.dumps(alert))
        r.zrem("jarvis:alert:active_set", alert_id)
        r.hincrby(STATS_KEY, "acknowledged", 1)


def get_active_alerts(min_severity: str = "info") -> list:
    min_idx = SEVERITIES.index(min_severity) if min_severity in SEVERITIES else 0
    alert_ids = r.zrangebyscore("jarvis:alert:active_set", min_idx, 10)
    alerts = []
    for aid in alert_ids:
        raw = r.get(f"{ALERT_PREFIX}{aid}")
        if raw:
            a = json.loads(raw)
            if not a.get("acknowledged"):
                alerts.append(a)
    return sorted(alerts, key=lambda x: -SEVERITIES.index(x.get("severity", "info")))


def alert_history(limit: int = 10) -> list:
    return [json.loads(x) for x in r.lrange(HISTORY_KEY, 0, limit - 1)]


def stats() -> dict:
    s = r.hgetall(STATS_KEY)
    return {k: (int(v) if v.isdigit() else v) for k, v in s.items()}


if __name__ == "__main__":
    register_rule("gpu_overheat", "gpu0_temp_c", "gt", 80.0, "critical", 300)
    register_rule("high_cpu", "cpu_pct", "gt", 85.0, "warn", 120)
    register_rule("m2_circuit_open", "m2_cb_open", "eq", 1.0, "error", 600)
    register_rule("low_free_vram", "gpu0_vram_free", "lt", 500, "warn", 180)
    register_rule("high_error_rate", "llm_error_pct", "gt", 20.0, "error", 300)

    # Test metrics
    metrics_scenarios = [
        {
            "name": "normal",
            "cpu_pct": 45,
            "gpu0_temp_c": 35,
            "m2_cb_open": 0,
            "gpu0_vram_free": 3000,
            "llm_error_pct": 2,
        },
        {
            "name": "stressed",
            "cpu_pct": 88,
            "gpu0_temp_c": 83,
            "m2_cb_open": 1,
            "gpu0_vram_free": 300,
            "llm_error_pct": 35,
        },
    ]

    for scenario in metrics_scenarios:
        name = scenario.pop("name")
        fired = evaluate_rules(scenario)
        print(f"\nScenario '{name}': {len(fired)} alerts fired")
        for alert in fired:
            sev_icon = {"info": "ℹ️", "warn": "⚠️", "error": "🔴", "critical": "💥"}.get(
                alert["severity"], "?"
            )
            print(f"  {sev_icon} [{alert['severity']:8s}] {alert['message']}")

    active = get_active_alerts("warn")
    print(f"\nActive alerts (≥warn): {len(active)}")

    if active:
        acknowledge(active[0]["id"], by="turbo")
        print(f"Acknowledged: {active[0]['rule']}")

    print(f"\nStats: {stats()}")
