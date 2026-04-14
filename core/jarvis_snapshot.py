#!/usr/bin/env python3
"""JARVIS Snapshot — Full system state snapshot with diff and restore capabilities"""

import redis
import json
import time
import hashlib

r = redis.Redis(decode_responses=True)

SNAP_PREFIX = "jarvis:snapshot:"
INDEX_KEY = "jarvis:snapshot:index"

CAPTURE_KEYS = [
    "jarvis:score",
    "jarvis:cluster:state",
    "jarvis:health_scorer:score",
    "jarvis:llm_router:*:count",
    "jarvis:cb:*",
    "jarvis:rate_optimizer:limits",
    "jarvis:model_registry",
]


def capture(label: str = "", tags: list = None) -> dict:
    """Capture current system state into a snapshot."""
    state = {}
    for pattern in CAPTURE_KEYS:
        if "*" in pattern:
            for key in r.scan_iter(pattern, count=100):
                try:
                    ktype = r.type(key)
                    if ktype == "string":
                        val = r.get(key)
                    elif ktype == "hash":
                        val = r.hgetall(key)
                    else:
                        continue
                    if val:
                        state[key] = val if isinstance(val, dict) else val
                except Exception:
                    continue
        else:
            try:
                val = r.get(pattern)
                if val:
                    state[pattern] = val
            except Exception:
                pass

    snap_id = hashlib.md5(f"{time.time()}{label}".encode()).hexdigest()[:12]
    snapshot = {
        "id": snap_id,
        "label": label or f"auto_{int(time.time())}",
        "tags": tags or [],
        "ts": time.time(),
        "keys_captured": len(state),
        "state": state,
    }
    r.setex(f"{SNAP_PREFIX}{snap_id}", 86400 * 7, json.dumps(snapshot))
    r.lpush(
        INDEX_KEY,
        json.dumps(
            {
                "id": snap_id,
                "label": snapshot["label"],
                "ts": snapshot["ts"],
                "keys": len(state),
            }
        ),
    )
    r.ltrim(INDEX_KEY, 0, 49)
    return snapshot


def list_snapshots(limit: int = 10) -> list:
    raw = r.lrange(INDEX_KEY, 0, limit - 1)
    return [json.loads(x) for x in raw]


def get(snap_id: str) -> dict | None:
    raw = r.get(f"{SNAP_PREFIX}{snap_id}")
    return json.loads(raw) if raw else None


def diff(snap_id_a: str, snap_id_b: str) -> dict:
    """Compare two snapshots, return changed keys."""
    a = get(snap_id_a)
    b = get(snap_id_b)
    if not a or not b:
        return {"error": "snapshot not found"}

    state_a = a["state"]
    state_b = b["state"]
    all_keys = set(state_a) | set(state_b)

    added = [k for k in all_keys if k in state_b and k not in state_a]
    removed = [k for k in all_keys if k in state_a and k not in state_b]
    changed = [
        k
        for k in all_keys
        if k in state_a and k in state_b and state_a[k] != state_b[k]
    ]

    return {
        "from": snap_id_a,
        "to": snap_id_b,
        "added": added,
        "removed": removed,
        "changed": changed,
        "delta_keys": len(added) + len(removed) + len(changed),
    }


def restore(snap_id: str, dry_run: bool = True) -> dict:
    """Restore Redis state from snapshot."""
    snap = get(snap_id)
    if not snap:
        return {"ok": False, "error": "snapshot not found"}

    restored = 0
    for key, val in snap["state"].items():
        if "*" in key:
            continue
        if not dry_run:
            if isinstance(val, dict):
                r.hset(key, mapping=val)
            else:
                r.setex(key, 3600, val)
        restored += 1

    return {
        "ok": True,
        "snap_id": snap_id,
        "keys_restored": restored,
        "dry_run": dry_run,
    }


def stats() -> dict:
    snaps = list_snapshots(50)
    return {"total_snapshots": len(snaps), "latest": snaps[0] if snaps else None}


if __name__ == "__main__":
    s1 = capture("before_test", tags=["auto"])
    print(f"Snapshot 1: {s1['id']} — {s1['keys_captured']} keys")
    # Mutate some state
    r.set("jarvis:score", json.dumps({"total": 95}))
    s2 = capture("after_mutation", tags=["auto"])
    print(f"Snapshot 2: {s2['id']} — {s2['keys_captured']} keys")
    d = diff(s1["id"], s2["id"])
    print(
        f"Diff: +{len(d['added'])} -{len(d['removed'])} ~{len(d['changed'])} keys changed"
    )
    print(f"Restore dry-run: {restore(s1['id'], dry_run=True)}")
    print(f"Snapshots available: {len(list_snapshots())}")
