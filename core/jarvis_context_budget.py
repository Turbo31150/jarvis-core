#!/usr/bin/env python3
"""
jarvis_context_budget — Token budget manager per session/agent
Tracks token usage, enforces limits, triggers compression when near ceiling
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.context_budget")

REDIS_PREFIX = "jarvis:budget:"
DEFAULT_BUDGET = 32768  # tokens
COMPRESSION_THRESHOLD = 0.80  # compress at 80% usage
HARD_LIMIT_RATIO = 0.95  # refuse new turns at 95%

# Model context windows
MODEL_LIMITS = {
    "qwen3.5-9b": 32768,
    "qwen3.5-35b-a3b": 32768,
    "qwen3.5-27b": 32768,
    "qwen3-14b": 32768,
    "deepseek-r1": 65536,
    "glm-4.7-flash": 32768,
    "gpt-oss-20b": 16384,
    "gemma3:4b": 8192,
}


@dataclass
class BudgetState:
    session_id: str
    model: str
    max_tokens: int
    used_tokens: int = 0
    turns: int = 0
    compressions: int = 0
    created_at: float = field(default_factory=time.time)
    last_update: float = field(default_factory=time.time)

    @property
    def remaining(self) -> int:
        return max(0, self.max_tokens - self.used_tokens)

    @property
    def usage_pct(self) -> float:
        return self.used_tokens / self.max_tokens if self.max_tokens > 0 else 0.0

    @property
    def pressure(self) -> str:
        p = self.usage_pct
        if p >= HARD_LIMIT_RATIO:
            return "HARD_LIMIT"
        if p >= COMPRESSION_THRESHOLD:
            return "COMPRESS"
        if p >= 0.60:
            return "HIGH"
        return "OK"

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "model": self.model,
            "max_tokens": self.max_tokens,
            "used_tokens": self.used_tokens,
            "remaining": self.remaining,
            "usage_pct": round(self.usage_pct * 100, 1),
            "turns": self.turns,
            "compressions": self.compressions,
            "pressure": self.pressure,
        }


class ContextBudgetManager:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._local: dict[str, BudgetState] = {}

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def _model_limit(self, model: str) -> int:
        for key, limit in MODEL_LIMITS.items():
            if key.lower() in model.lower():
                return limit
        return DEFAULT_BUDGET

    async def _save(self, state: BudgetState):
        self._local[state.session_id] = state
        if self.redis:
            await self.redis.set(
                f"{REDIS_PREFIX}{state.session_id}",
                json.dumps(state.to_dict()),
                ex=3600,
            )

    async def _load(self, session_id: str) -> BudgetState | None:
        if session_id in self._local:
            return self._local[session_id]
        if self.redis:
            raw = await self.redis.get(f"{REDIS_PREFIX}{session_id}")
            if raw:
                d = json.loads(raw)
                s = BudgetState(
                    session_id=d["session_id"],
                    model=d["model"],
                    max_tokens=d["max_tokens"],
                    used_tokens=d["used_tokens"],
                    turns=d["turns"],
                    compressions=d["compressions"],
                )
                self._local[session_id] = s
                return s
        return None

    async def create(self, session_id: str, model: str = "qwen3.5-9b") -> BudgetState:
        limit = self._model_limit(model)
        state = BudgetState(
            session_id=session_id,
            model=model,
            max_tokens=limit,
        )
        await self._save(state)
        log.info(f"Budget created: {session_id} model={model} limit={limit}")
        return state

    async def record_turn(
        self,
        session_id: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> BudgetState:
        state = await self._load(session_id)
        if not state:
            state = await self.create(session_id)

        state.used_tokens += prompt_tokens + completion_tokens
        state.turns += 1
        state.last_update = time.time()
        await self._save(state)

        if state.pressure in ("COMPRESS", "HARD_LIMIT"):
            log.warning(
                f"Budget pressure [{state.pressure}] "
                f"{session_id}: {state.usage_pct * 100:.0f}%"
            )
            if self.redis:
                await self.redis.publish(
                    "jarvis:events",
                    json.dumps(
                        {
                            "event": "budget_pressure",
                            "session_id": session_id,
                            "pressure": state.pressure,
                            "usage_pct": round(state.usage_pct * 100, 1),
                        }
                    ),
                )

        return state

    async def record_compression(
        self, session_id: str, tokens_freed: int
    ) -> BudgetState:
        state = await self._load(session_id)
        if not state:
            return await self.create(session_id)

        state.used_tokens = max(0, state.used_tokens - tokens_freed)
        state.compressions += 1
        state.last_update = time.time()
        await self._save(state)
        log.info(
            f"Compression {session_id}: freed {tokens_freed} tokens, "
            f"now at {state.usage_pct * 100:.0f}%"
        )
        return state

    async def get(self, session_id: str) -> BudgetState | None:
        return await self._load(session_id)

    async def list_active(self) -> list[dict]:
        if not self.redis:
            return [s.to_dict() for s in self._local.values()]
        keys = await self.redis.keys(f"{REDIS_PREFIX}*")
        result = []
        for k in keys:
            raw = await self.redis.get(k)
            if raw:
                result.append(json.loads(raw))
        return sorted(result, key=lambda x: -x["usage_pct"])

    async def reset(self, session_id: str):
        self._local.pop(session_id, None)
        if self.redis:
            await self.redis.delete(f"{REDIS_PREFIX}{session_id}")
        log.info(f"Budget reset: {session_id}")


# ── CLI ───────────────────────────────────────────────────────────────────────


async def main():
    import sys

    mgr = ContextBudgetManager()
    await mgr.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"

    if cmd == "list":
        sessions = await mgr.list_active()
        if not sessions:
            print("No active budget sessions")
        else:
            print(
                f"{'Session':<24} {'Model':<20} {'Used%':>6} {'Turns':>6} {'Pressure':<12}"
            )
            print("-" * 75)
            for s in sessions:
                print(
                    f"{s['session_id']:<24} {s['model']:<20} "
                    f"{s['usage_pct']:>5.1f}% {s['turns']:>6} {s['pressure']:<12}"
                )

    elif cmd == "get" and len(sys.argv) > 2:
        state = await mgr.get(sys.argv[2])
        if state:
            print(json.dumps(state.to_dict(), indent=2))
        else:
            print(f"Session '{sys.argv[2]}' not found")

    elif cmd == "create" and len(sys.argv) > 2:
        sid = sys.argv[2]
        model = sys.argv[3] if len(sys.argv) > 3 else "qwen3.5-9b"
        state = await mgr.create(sid, model)
        print(json.dumps(state.to_dict(), indent=2))

    elif cmd == "reset" and len(sys.argv) > 2:
        await mgr.reset(sys.argv[2])
        print(f"Reset: {sys.argv[2]}")


if __name__ == "__main__":
    asyncio.run(main())
