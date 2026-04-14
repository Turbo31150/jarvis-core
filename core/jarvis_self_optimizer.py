#!/usr/bin/env python3
"""JARVIS Self Optimizer — Analyze system metrics and apply auto-tuning rules"""

import redis
import json
import time

r = redis.Redis(decode_responses=True)
RULES_KEY = "jarvis:self_opt:rules"
ACTIONS_KEY = "jarvis:self_opt:actions"
STATS_KEY = "jarvis:self_opt:stats"

# Auto-tuning rules: condition → action
RULES = [
    {
        "id": "throttle_m2_on_errors",
        "desc": "Reduce M2 rate limit when error rate > 20%",
        "condition": {
            "metric": "jarvis:llm_router:default:errors",
            "op": "gt",
            "threshold": 5,
        },
        "action": {
            "type": "redis_set",
            "key": "jarvis:rate_opt:m2:rps_override",
            "value": "3",
            "ttl": 300,
        },
    },
    {
        "id": "scale_ol1_on_load",
        "desc": "Boost OL1 weight when M2 is slow",
        "condition": {
            "metric": "jarvis:router_v2:m2:ewa_lat",
            "op": "gt",
            "threshold": 2000,
        },
        "action": {
            "type": "redis_set",
            "key": "jarvis:balancer:ol1:weight_boost",
            "value": "2",
            "ttl": 600,
        },
    },
    {
        "id": "cache_warmup_on_restart",
        "desc": "Trigger cache warmup when hit rate < 30%",
        "condition": {
            "metric": "jarvis:pcache:stats",
            "field": "hit_rate",
            "op": "lt",
            "threshold": 0.3,
        },
        "action": {
            "type": "function",
            "module": "jarvis_prompt_cache",
            "func": "warmup",
            "args": [],
        },
    },
    {
        "id": "snapshot_on_high_score",
        "desc": "Auto-snapshot when score > 90",
        "condition": {
            "metric": "jarvis:score",
            "field": "total",
            "op": "gt",
            "threshold": 90,
        },
        "action": {
            "type": "function",
            "module": "jarvis_snapshot",
            "func": "capture",
            "args": ["auto_high_score"],
        },
    },
    {
        "id": "alert_on_gpu_heat",
        "desc": "Alert when any GPU > 80°C",
        "condition": {"metric": "jarvis:gpu:0:temp", "op": "gt", "threshold": 80},
        "action": {
            "type": "publish",
            "channel": "jarvis:alerts",
            "payload": {"severity": "warn", "msg": "GPU temperature critical"},
        },
    },
]


def _get_metric(condition: dict) -> float | None:
    key = condition["metric"]
    field = condition.get("field")
    try:
        raw = r.get(key)
        if raw:
            if field:
                data = json.loads(raw)
                return float(data.get(field, 0))
            return float(raw)
    except Exception:
        pass
    return None


def _check_condition(condition: dict) -> bool:
    val = _get_metric(condition)
    if val is None:
        return False
    op = condition["op"]
    threshold = condition["threshold"]
    if op == "gt":
        return val > threshold
    elif op == "lt":
        return val < threshold
    elif op == "eq":
        return val == threshold
    elif op == "gte":
        return val >= threshold
    elif op == "lte":
        return val <= threshold
    return False


def _apply_action(action: dict) -> dict:
    atype = action["type"]
    try:
        if atype == "redis_set":
            r.setex(action["key"], int(action.get("ttl", 300)), action["value"])
            return {"ok": True, "applied": f"set {action['key']}={action['value']}"}
        elif atype == "publish":
            r.publish(action["channel"], json.dumps(action["payload"]))
            return {"ok": True, "applied": f"published to {action['channel']}"}
        elif atype == "function":
            import importlib

            mod = importlib.import_module(action["module"])
            fn = getattr(mod, action["func"])
            result = fn(*action.get("args", []))
            return {
                "ok": True,
                "applied": f"{action['module']}.{action['func']}",
                "result": str(result)[:80],
            }
    except Exception as e:
        return {"ok": False, "error": str(e)[:80]}
    return {"ok": False, "error": "unknown action type"}


def run_cycle() -> dict:
    """Run one optimization cycle: check all rules, apply triggered ones."""
    triggered = []
    applied = []

    for rule in RULES:
        if _check_condition(rule["condition"]):
            triggered.append(rule["id"])
            # Dedup: don't re-apply same rule within cooldown
            cooldown_key = f"jarvis:self_opt:cooldown:{rule['id']}"
            if r.exists(cooldown_key):
                continue
            result = _apply_action(rule["action"])
            r.setex(cooldown_key, 120, "1")  # 2min cooldown
            applied.append({"rule": rule["id"], "desc": rule["desc"], **result})
            r.lpush(ACTIONS_KEY, json.dumps({**applied[-1], "ts": time.time()}))
            r.ltrim(ACTIONS_KEY, 0, 99)

    r.hincrby(STATS_KEY, "cycles", 1)
    r.hincrby(STATS_KEY, "triggered", len(triggered))
    r.hincrby(STATS_KEY, "applied", len(applied))
    return {"triggered": triggered, "applied": applied, "ts": time.time()}


def recent_actions(limit: int = 10) -> list:
    raw = r.lrange(ACTIONS_KEY, 0, limit - 1)
    return [json.loads(x) for x in raw]


def stats() -> dict:
    s = r.hgetall(STATS_KEY)
    return {
        "rules": len(RULES),
        **{k: int(v) for k, v in s.items()},
        "recent_actions": recent_actions(5),
    }


if __name__ == "__main__":
    # Seed some triggerable metrics
    r.set("jarvis:llm_router:default:errors", "8")
    r.set("jarvis:router_v2:m2:ewa_lat", "2500")
    r.set("jarvis:score", json.dumps({"total": 95}))

    result = run_cycle()
    print(
        f"Optimization cycle: {len(result['triggered'])} triggered, {len(result['applied'])} applied"
    )
    for action in result["applied"]:
        icon = "✅" if action["ok"] else "❌"
        print(f"  {icon} [{action['rule']}] {action['desc']}")
    print(f"\nStats: {stats()}")
