#!/usr/bin/env python3
"""JARVIS Workflow Monitor — Real-time tracking of running workflows and steps"""

import redis
import json
import time
import uuid

r = redis.Redis(decode_responses=True)

WF_PREFIX = "jarvis:wf_monitor:"
ACTIVE_KEY = "jarvis:wf_monitor:active"
HISTORY_KEY = "jarvis:wf_monitor:history"


def start_workflow(name: str, steps: list, metadata: dict = None) -> str:
    wid = f"{name}_{uuid.uuid4().hex[:8]}"
    payload = {
        "id": wid,
        "name": name,
        "steps": steps,
        "current_step": 0,
        "status": "running",
        "started_at": time.time(),
        "metadata": metadata or {},
        "step_results": [],
    }
    r.setex(f"{WF_PREFIX}{wid}", 3600, json.dumps(payload))
    r.sadd(ACTIVE_KEY, wid)
    return wid


def update_step(
    wid: str, step_idx: int, status: str, result: str = "", duration_ms: float = 0
):
    raw = r.get(f"{WF_PREFIX}{wid}")
    if not raw:
        return False
    wf = json.loads(raw)
    wf["current_step"] = step_idx
    wf["step_results"].append(
        {
            "step": step_idx,
            "name": wf["steps"][step_idx] if step_idx < len(wf["steps"]) else "?",
            "status": status,
            "result": result[:200],
            "duration_ms": duration_ms,
            "ts": time.time(),
        }
    )
    r.setex(f"{WF_PREFIX}{wid}", 3600, json.dumps(wf))
    return True


def finish_workflow(wid: str, status: str = "done", error: str = ""):
    raw = r.get(f"{WF_PREFIX}{wid}")
    if not raw:
        return False
    wf = json.loads(raw)
    wf["status"] = status
    wf["finished_at"] = time.time()
    wf["duration_s"] = round(wf["finished_at"] - wf["started_at"], 2)
    wf["error"] = error
    # Archive to history (keep last 100)
    r.lpush(HISTORY_KEY, json.dumps(wf))
    r.ltrim(HISTORY_KEY, 0, 99)
    r.delete(f"{WF_PREFIX}{wid}")
    r.srem(ACTIVE_KEY, wid)
    return True


def get_active() -> list:
    wids = r.smembers(ACTIVE_KEY)
    result = []
    for wid in wids:
        raw = r.get(f"{WF_PREFIX}{wid}")
        if raw:
            result.append(json.loads(raw))
        else:
            r.srem(ACTIVE_KEY, wid)
    return sorted(result, key=lambda x: x["started_at"], reverse=True)


def get_history(limit: int = 20) -> list:
    raw = r.lrange(HISTORY_KEY, 0, limit - 1)
    return [json.loads(x) for x in raw]


def stats() -> dict:
    history = get_history(100)
    done = [h for h in history if h["status"] == "done"]
    failed = [h for h in history if h["status"] == "failed"]
    avg_dur = round(sum(h.get("duration_s", 0) for h in done) / max(len(done), 1), 2)
    return {
        "active": len(r.smembers(ACTIVE_KEY)),
        "history_total": len(history),
        "success_rate": round(len(done) / max(len(history), 1), 2),
        "avg_duration_s": avg_dur,
        "failed_count": len(failed),
    }


if __name__ == "__main__":
    steps = ["fetch_data", "process", "validate", "store", "notify"]
    wid = start_workflow("data_pipeline", steps, {"source": "redis"})
    print(f"Started: {wid}")
    for i, step in enumerate(steps):
        update_step(wid, i, "ok", f"completed {step}", 120.0 + i * 30)
    finish_workflow(wid, "done")
    print(f"Stats: {stats()}")
    hist = get_history(1)
    if hist:
        wf = hist[0]
        print(
            f"Last: {wf['name']} — {wf['status']} in {wf['duration_s']}s, {len(wf['step_results'])} steps"
        )
