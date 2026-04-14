#!/usr/bin/env python3
"""JARVIS Loop Controller — Control and monitor continuous background loops"""

import redis
import json
import time
import threading

r = redis.Redis(decode_responses=True)

LOOPS_KEY = "jarvis:loops:registry"
HEARTBEAT_PREFIX = "jarvis:loops:hb:"
STATS_KEY = "jarvis:loops:stats"

_active_threads: dict = {}
_stop_events: dict = {}


def register_loop(
    loop_id: str, interval_s: float, desc: str = "", owner: str = "system"
) -> dict:
    entry = {
        "id": loop_id,
        "interval_s": interval_s,
        "desc": desc,
        "owner": owner,
        "registered_at": time.time(),
        "status": "registered",
    }
    r.hset(LOOPS_KEY, loop_id, json.dumps(entry))
    return entry


def heartbeat(loop_id: str, metadata: dict = None):
    """Called by a loop at each iteration to prove it's alive."""
    r.setex(
        f"{HEARTBEAT_PREFIX}{loop_id}",
        int(60 + 10),
        json.dumps({"ts": time.time(), "metadata": metadata or {}}),
    )
    r.hincrby(STATS_KEY, f"{loop_id}:ticks", 1)


def is_alive(loop_id: str) -> bool:
    return r.exists(f"{HEARTBEAT_PREFIX}{loop_id}") > 0


def get_status(loop_id: str) -> dict:
    raw = r.hget(LOOPS_KEY, loop_id)
    if not raw:
        return {"id": loop_id, "status": "unknown"}
    entry = json.loads(raw)
    hb_raw = r.get(f"{HEARTBEAT_PREFIX}{loop_id}")
    hb = json.loads(hb_raw) if hb_raw else {}
    ticks = int(r.hget(STATS_KEY, f"{loop_id}:ticks") or 0)
    return {
        **entry,
        "alive": is_alive(loop_id),
        "last_heartbeat": hb.get("ts"),
        "age_s": round(time.time() - hb["ts"], 1) if hb.get("ts") else None,
        "ticks": ticks,
    }


def start_loop(loop_id: str, fn, interval_s: float = None, args: tuple = ()):
    """Start a background loop thread."""
    if loop_id in _active_threads and _active_threads[loop_id].is_alive():
        return {"ok": False, "reason": "already running"}

    stop_event = threading.Event()
    _stop_events[loop_id] = stop_event

    raw = r.hget(LOOPS_KEY, loop_id)
    ivl = interval_s or (json.loads(raw)["interval_s"] if raw else 60)

    def _run():
        while not stop_event.is_set():
            try:
                fn(*args)
                heartbeat(loop_id)
            except Exception:
                r.hincrby(STATS_KEY, f"{loop_id}:errors", 1)
            stop_event.wait(ivl)

    t = threading.Thread(target=_run, name=f"loop:{loop_id}", daemon=True)
    _active_threads[loop_id] = t
    t.start()

    # Update status
    if raw:
        entry = json.loads(raw)
        entry["status"] = "running"
        r.hset(LOOPS_KEY, loop_id, json.dumps(entry))

    return {"ok": True, "loop_id": loop_id, "interval_s": ivl}


def stop_loop(loop_id: str) -> bool:
    ev = _stop_events.get(loop_id)
    if ev:
        ev.set()
    raw = r.hget(LOOPS_KEY, loop_id)
    if raw:
        entry = json.loads(raw)
        entry["status"] = "stopped"
        r.hset(LOOPS_KEY, loop_id, json.dumps(entry))
    return True


def list_loops() -> list:
    return [get_status(lid) for lid in r.hkeys(LOOPS_KEY)]


def watchdog_check() -> list:
    """Find loops that should be alive but aren't heartbeating."""
    dead = []
    for loop_id in r.hkeys(LOOPS_KEY):
        status = get_status(loop_id)
        if status.get("status") == "running" and not status.get("alive"):
            dead.append(loop_id)
            r.hincrby(STATS_KEY, f"{loop_id}:missed_heartbeats", 1)
    return dead


def stats() -> dict:
    loops = list_loops()
    running = sum(1 for l in loops if l.get("alive"))
    return {"total_loops": len(loops), "alive": running, "dead": len(loops) - running}


if __name__ == "__main__":
    import itertools

    counter = itertools.count(1)

    register_loop("test_loop_1", interval_s=0.1, desc="Test counter loop")
    register_loop("test_loop_2", interval_s=0.2, desc="Test metrics loop")
    register_loop("orphan_loop", interval_s=5.0, desc="Registered but not started")

    def tick():
        n = next(counter)
        r.set("jarvis:loop_test:counter", n)

    start_loop("test_loop_1", tick, interval_s=0.05)
    start_loop(
        "test_loop_2", lambda: r.set("jarvis:loop_test:ts", time.time()), interval_s=0.1
    )

    time.sleep(0.5)

    print("Loop status:")
    for loop in list_loops():
        icon = "🟢" if loop.get("alive") else "🔴"
        print(
            f"  {icon} {loop['id']:20s} ticks={loop.get('ticks', 0)} alive={loop.get('alive')}"
        )

    dead = watchdog_check()
    print(f"\nDead loops: {dead}")
    print(f"Counter value: {r.get('jarvis:loop_test:counter')}")

    stop_loop("test_loop_1")
    stop_loop("test_loop_2")
    print(f"Stats: {stats()}")
