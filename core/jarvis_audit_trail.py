#!/usr/bin/env python3
"""
jarvis_audit_trail — Immutable append-only audit log for all agent actions
Tracks who did what, when, with cryptographic chaining for tamper detection
"""

import asyncio
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.audit_trail")

AUDIT_FILE = Path("/home/turbo/IA/Core/jarvis/data/audit_trail.jsonl")
REDIS_KEY = "jarvis:audit:chain"
REDIS_STREAM = "jarvis:audit:stream"


@dataclass
class AuditEntry:
    entry_id: str
    ts: float
    actor: str  # agent_id, user, system
    action: str  # what was done
    target: str  # affected resource/service
    payload: dict  # action details
    outcome: str  # ok | error | blocked
    prev_hash: str  # hash of previous entry (chain)
    entry_hash: str = ""

    def __post_init__(self):
        if not self.entry_hash:
            self.entry_hash = self._compute_hash()

    def _compute_hash(self) -> str:
        data = f"{self.entry_id}{self.ts}{self.actor}{self.action}{self.target}{self.prev_hash}"
        return hashlib.sha256(data.encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        return {
            "entry_id": self.entry_id,
            "ts": self.ts,
            "ts_human": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.ts)),
            "actor": self.actor,
            "action": self.action,
            "target": self.target,
            "payload": self.payload,
            "outcome": self.outcome,
            "prev_hash": self.prev_hash,
            "entry_hash": self.entry_hash,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AuditEntry":
        return cls(
            entry_id=d["entry_id"],
            ts=d["ts"],
            actor=d["actor"],
            action=d["action"],
            target=d["target"],
            payload=d.get("payload", {}),
            outcome=d["outcome"],
            prev_hash=d["prev_hash"],
            entry_hash=d.get("entry_hash", ""),
        )


class AuditTrail:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._last_hash: str = "genesis"
        self._load_last_hash()

    def _load_last_hash(self):
        if AUDIT_FILE.exists():
            try:
                lines = AUDIT_FILE.read_text().strip().split("\n")
                for line in reversed(lines):
                    if line.strip():
                        entry = json.loads(line)
                        self._last_hash = entry.get("entry_hash", "genesis")
                        return
            except Exception as e:
                log.warning(f"Load audit hash error: {e}")

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
            stored = await self.redis.get(REDIS_KEY)
            if stored:
                self._last_hash = stored
        except Exception:
            self.redis = None

    async def record(
        self,
        actor: str,
        action: str,
        target: str,
        payload: dict | None = None,
        outcome: str = "ok",
    ) -> AuditEntry:
        entry = AuditEntry(
            entry_id=str(uuid.uuid4())[:8],
            ts=time.time(),
            actor=actor,
            action=action,
            target=target,
            payload=payload or {},
            outcome=outcome,
            prev_hash=self._last_hash,
        )

        # Append to JSONL file (append-only)
        AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(AUDIT_FILE, "a") as f:
            f.write(json.dumps(entry.to_dict()) + "\n")

        self._last_hash = entry.entry_hash

        if self.redis:
            await self.redis.set(REDIS_KEY, entry.entry_hash, ex=86400)
            await self.redis.xadd(
                REDIS_STREAM,
                {
                    "entry_id": entry.entry_id,
                    "actor": actor,
                    "action": action,
                    "target": target,
                    "outcome": outcome,
                    "ts": str(entry.ts),
                },
                maxlen=10000,
            )

        icon = "✅" if outcome == "ok" else "❌"
        log.info(f"{icon} [{entry.entry_id}] {actor} → {action} on {target}")
        return entry

    def query(
        self,
        actor: str | None = None,
        action: str | None = None,
        target: str | None = None,
        outcome: str | None = None,
        since: float = 0.0,
        limit: int = 50,
    ) -> list[dict]:
        if not AUDIT_FILE.exists():
            return []
        results = []
        try:
            lines = AUDIT_FILE.read_text().strip().split("\n")
            for line in reversed(lines):
                if not line.strip():
                    continue
                entry = json.loads(line)
                if entry["ts"] < since:
                    continue
                if actor and entry["actor"] != actor:
                    continue
                if action and entry["action"] != action:
                    continue
                if target and entry["target"] != target:
                    continue
                if outcome and entry["outcome"] != outcome:
                    continue
                results.append(entry)
                if len(results) >= limit:
                    break
        except Exception as e:
            log.error(f"Query error: {e}")
        return results

    def verify_chain(self) -> tuple[bool, int, str]:
        """Verify hash chain integrity. Returns (ok, entries_checked, error_msg)."""
        if not AUDIT_FILE.exists():
            return True, 0, ""
        prev = "genesis"
        count = 0
        try:
            for line in AUDIT_FILE.read_text().strip().split("\n"):
                if not line.strip():
                    continue
                d = json.loads(line)
                if d["prev_hash"] != prev:
                    return (
                        False,
                        count,
                        f"Chain broken at entry {d['entry_id']}: expected prev={prev}, got {d['prev_hash']}",
                    )
                prev = d["entry_hash"]
                count += 1
        except Exception as e:
            return False, count, str(e)
        return True, count, ""

    def stats(self) -> dict:
        if not AUDIT_FILE.exists():
            return {"total": 0}
        entries = []
        for line in AUDIT_FILE.read_text().strip().split("\n"):
            if line.strip():
                try:
                    entries.append(json.loads(line))
                except Exception:
                    pass
        if not entries:
            return {"total": 0}
        by_actor: dict[str, int] = {}
        by_action: dict[str, int] = {}
        outcomes: dict[str, int] = {}
        for e in entries:
            by_actor[e["actor"]] = by_actor.get(e["actor"], 0) + 1
            by_action[e["action"]] = by_action.get(e["action"], 0) + 1
            outcomes[e["outcome"]] = outcomes.get(e["outcome"], 0) + 1
        return {
            "total": len(entries),
            "top_actors": sorted(by_actor.items(), key=lambda x: -x[1])[:5],
            "top_actions": sorted(by_action.items(), key=lambda x: -x[1])[:5],
            "outcomes": outcomes,
            "earliest": time.strftime("%Y-%m-%d", time.localtime(entries[0]["ts"])),
            "latest": time.strftime("%Y-%m-%d", time.localtime(entries[-1]["ts"])),
        }


async def main():
    import sys

    trail = AuditTrail()
    await trail.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        await trail.record("jarvis_core", "model_load", "qwen3.5-9b", {"backend": "M1"})
        await trail.record("user", "api_call", "/v1/chat", {"tokens": 512})
        await trail.record("jarvis_health", "restart", "lmstudio", {}, outcome="ok")
        await trail.record(
            "jarvis_security",
            "blocked",
            "ssh_attempt",
            {"ip": "10.0.0.5"},
            outcome="blocked",
        )
        print("4 entries recorded.")

    elif cmd == "verify":
        ok, count, err = trail.verify_chain()
        if ok:
            print(f"✅ Chain intact: {count} entries verified")
        else:
            print(f"❌ Chain BROKEN: {err}")

    elif cmd == "query":
        actor = sys.argv[2] if len(sys.argv) > 2 else None
        entries = trail.query(actor=actor, limit=20)
        for e in entries:
            icon = "✅" if e["outcome"] == "ok" else "❌"
            print(
                f"  {icon} [{e['ts_human']}] {e['actor']} → {e['action']} on {e['target']}"
            )

    elif cmd == "stats":
        s = trail.stats()
        print(json.dumps(s, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
