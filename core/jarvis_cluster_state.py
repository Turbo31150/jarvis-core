#!/usr/bin/env python3
"""JARVIS Cluster State — Centralized cluster state machine with event-driven transitions"""

import redis
import json
import time

r = redis.Redis(decode_responses=True)

STATE_KEY = "jarvis:cluster:state"
HISTORY_KEY = "jarvis:cluster:state_history"
NODES_KEY = "jarvis:cluster:nodes"

# Valid states and allowed transitions
STATES = {
    "idle": ["running", "degraded", "maintenance"],
    "running": ["idle", "degraded", "overloaded", "maintenance"],
    "degraded": ["running", "recovering", "failed"],
    "overloaded": ["running", "degraded", "backpressure"],
    "backpressure": ["running", "degraded"],
    "recovering": ["running", "degraded", "failed"],
    "maintenance": ["idle", "running"],
    "failed": ["recovering", "maintenance"],
}


def get_state() -> dict:
    raw = r.get(STATE_KEY)
    if raw:
        return json.loads(raw)
    return {"state": "idle", "since": time.time(), "reason": "init", "metadata": {}}


def transition(new_state: str, reason: str = "", metadata: dict = None) -> dict:
    current = get_state()
    cur_state = current["state"]

    allowed = STATES.get(cur_state, [])
    if new_state not in allowed:
        return {
            "ok": False,
            "error": f"invalid transition: {cur_state} → {new_state}",
            "allowed": allowed,
        }

    now = time.time()
    duration = round(now - current["since"], 1)

    # Archive old state
    record = {**current, "duration_s": duration, "transitioned_at": now}
    r.lpush(HISTORY_KEY, json.dumps(record))
    r.ltrim(HISTORY_KEY, 0, 99)

    new = {
        "state": new_state,
        "since": now,
        "reason": reason,
        "metadata": metadata or {},
    }
    r.set(STATE_KEY, json.dumps(new))

    # Publish event
    r.publish(
        "jarvis:cluster:events",
        json.dumps(
            {
                "type": "state_change",
                "from": cur_state,
                "to": new_state,
                "reason": reason,
                "ts": now,
            }
        ),
    )

    return {"ok": True, "from": cur_state, "to": new_state, "prev_duration_s": duration}


def register_node(node: str, status: str, info: dict = None):
    r.hset(
        NODES_KEY,
        node,
        json.dumps({"status": status, "info": info or {}, "ts": time.time()}),
    )


def get_nodes() -> dict:
    raw = r.hgetall(NODES_KEY)
    return {k: json.loads(v) for k, v in raw.items()}


def health_check() -> dict:
    nodes = get_nodes()
    up = sum(1 for n in nodes.values() if n["status"] == "up")
    total = len(nodes)
    state = get_state()

    # Auto-transition suggestions
    suggestions = []
    if total > 0 and up < total * 0.5 and state["state"] == "running":
        suggestions.append("consider transition to 'degraded'")
    if up == total and state["state"] == "degraded":
        suggestions.append("all nodes up, consider transition to 'running'")

    return {
        "state": state["state"],
        "nodes": {"up": up, "total": total},
        "suggestions": suggestions,
    }


def history(limit: int = 10) -> list:
    raw = r.lrange(HISTORY_KEY, 0, limit - 1)
    return [json.loads(x) for x in raw]


def stats() -> dict:
    return {"current": get_state(), "nodes": get_nodes(), "health": health_check()}


if __name__ == "__main__":
    # Register nodes
    for node, status in [
        ("M1", "up"),
        ("M2", "up"),
        ("M32", "up"),
        ("OL1", "up"),
        ("M3", "down"),
    ]:
        register_node(node, status)

    print(f"Initial: {get_state()['state']}")
    for transition_to, reason in [
        ("running", "all nodes ready"),
        ("overloaded", "GPU load > 90%"),
        ("backpressure", "queue depth > 1000"),
        ("running", "load normalized"),
    ]:
        result = transition(transition_to, reason)
        icon = "✅" if result["ok"] else "❌"
        print(f"  {icon} → {transition_to}: {result.get('error', 'ok')}")

    print(f"Health: {health_check()}")
    print(f"History: {len(history())} transitions")
