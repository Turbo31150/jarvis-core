#!/usr/bin/env python3
"""JARVIS Adaptive Scheduler — Dynamic task scheduling based on system load"""

import redis
import time
import json
from datetime import datetime

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:scheduler"

# Schedule rules: condition → action
RULES = [
    {
        "name": "gpu_cooldown",
        "trigger": "gpu_max_temp > 78",
        "action": "defer_gpu_tasks",
        "cooldown_s": 300,
    },
    {
        "name": "ram_pressure",
        "trigger": "ram_free_gb < 4",
        "action": "defer_background_tasks",
        "cooldown_s": 120,
    },
    {
        "name": "llm_overload",
        "trigger": "llm_queue_depth > 10",
        "action": "throttle_llm_requests",
        "cooldown_s": 60,
    },
    {
        "name": "night_mode",
        "trigger": "hour >= 0 and hour < 6",
        "action": "background_only",
        "cooldown_s": 3600,
    },
]


def get_system_state() -> dict:
    score = json.loads(r.get("jarvis:score") or "{}")
    gpu_max = float(r.get("jarvis:gpu:max_temp") or score.get("gpu_max_temp", 35))
    ram_free = float(score.get("ram_free_gb", 20))
    llm_queue = int(r.llen("jarvis:task_queue:llm") or 0)
    hour = datetime.now().hour
    return {
        "gpu_max_temp": gpu_max,
        "ram_free_gb": ram_free,
        "llm_queue_depth": llm_queue,
        "hour": hour,
    }


def evaluate_rules(state: dict) -> list[dict]:
    triggered = []
    for rule in RULES:
        try:
            cond = rule["trigger"]
            # Simple eval with state as locals
            if eval(cond, {"__builtins__": {}}, state):
                last_key = f"{PREFIX}:last:{rule['name']}"
                last = float(r.get(last_key) or 0)
                if time.time() - last > rule["cooldown_s"]:
                    r.setex(last_key, rule["cooldown_s"] * 2, time.time())
                    triggered.append(rule)
        except Exception:
            pass
    return triggered


def apply_actions(triggered: list[dict]) -> list[str]:
    actions_taken = []
    for rule in triggered:
        action = rule["action"]
        if action == "defer_gpu_tasks":
            r.setex(f"{PREFIX}:defer:gpu", 300, "1")
            actions_taken.append(f"GPU tasks deferred 5min (temp > 78°C)")
        elif action == "defer_background_tasks":
            r.setex(f"{PREFIX}:defer:background", 120, "1")
            actions_taken.append(f"Background tasks deferred 2min (RAM < 4GB)")
        elif action == "throttle_llm_requests":
            r.setex(f"{PREFIX}:throttle:llm", 60, "1")
            actions_taken.append(f"LLM throttled 1min (queue > 10)")
        elif action == "background_only":
            r.setex(f"{PREFIX}:mode:night", 3600, "1")
            actions_taken.append(f"Night mode: background tasks only")
    return actions_taken


def can_run(task_type: str) -> tuple[bool, str]:
    """Check if a task can run given current scheduler state"""
    if r.get(f"{PREFIX}:defer:gpu") and task_type in ("gpu", "inference", "training"):
        return False, "gpu_cooldown"
    if r.get(f"{PREFIX}:defer:background") and task_type == "background":
        return False, "ram_pressure"
    if r.get(f"{PREFIX}:throttle:llm") and task_type in ("llm", "classify", "summarize"):
        return False, "llm_throttled"
    if r.get(f"{PREFIX}:mode:night") and task_type not in ("background", "monitoring"):
        return False, "night_mode"
    return True, "ok"


def tick() -> dict:
    state = get_system_state()
    triggered = evaluate_rules(state)
    actions = apply_actions(triggered)
    result = {
        "ts": datetime.now().isoformat()[:19],
        "state": state,
        "rules_triggered": len(triggered),
        "actions": actions,
    }
    r.setex(f"{PREFIX}:last_tick", 120, json.dumps(result))
    return result


if __name__ == "__main__":
    import sys
    if "--watch" in sys.argv:
        print(f"[AdaptiveScheduler] Watching every 30s...")
        while True:
            res = tick()
            if res["actions"]:
                print(f"[{res['ts']}] Actions: {res['actions']}")
            time.sleep(30)
    else:
        res = tick()
        print(f"State: {res['state']}")
        print(f"Rules triggered: {res['rules_triggered']}")
        if res["actions"]:
            print(f"Actions: {res['actions']}")
        else:
            print("No actions needed")
        # Test can_run
        for t in ["gpu", "llm", "background", "monitoring"]:
            ok, reason = can_run(t)
            print(f"  can_run({t}): {ok} ({reason})")
