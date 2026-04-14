#!/usr/bin/env python3
"""
jarvis_pipeline_debugger — Step-by-step pipeline execution debugger
Captures intermediate states, diffs, timings, and anomalies across pipeline stages
"""

import asyncio
import json
import logging
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.pipeline_debugger")

DEBUG_DIR = Path("/home/turbo/IA/Core/jarvis/data/debug")
REDIS_PREFIX = "jarvis:dbg:"


@dataclass
class StepRecord:
    step_id: str
    name: str
    input_repr: str
    output_repr: str
    duration_ms: float
    status: str  # ok | error | skipped | slow
    error: str = ""
    metadata: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    @property
    def slow(self) -> bool:
        return self.duration_ms > 2000

    def to_dict(self) -> dict:
        return {
            "step_id": self.step_id,
            "name": self.name,
            "input_repr": self.input_repr[:200],
            "output_repr": self.output_repr[:200],
            "duration_ms": round(self.duration_ms, 1),
            "status": self.status,
            "error": self.error,
            "metadata": self.metadata,
            "ts": self.ts,
        }


@dataclass
class PipelineDebugSession:
    session_id: str
    pipeline_name: str
    steps: list[StepRecord] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    ended_at: float = 0.0
    status: str = "running"

    @property
    def total_ms(self) -> float:
        return sum(s.duration_ms for s in self.steps)

    @property
    def bottleneck(self) -> StepRecord | None:
        return max(self.steps, key=lambda s: s.duration_ms) if self.steps else None

    @property
    def errors(self) -> list[StepRecord]:
        return [s for s in self.steps if s.status == "error"]

    def to_dict(self) -> dict:
        bn = self.bottleneck
        return {
            "session_id": self.session_id,
            "pipeline_name": self.pipeline_name,
            "status": self.status,
            "step_count": len(self.steps),
            "total_ms": round(self.total_ms, 1),
            "error_count": len(self.errors),
            "bottleneck": bn.name if bn else None,
            "bottleneck_ms": round(bn.duration_ms, 1) if bn else 0,
            "started_at": self.started_at,
        }


def _repr(value: Any, max_len: int = 200) -> str:
    try:
        if isinstance(value, str):
            return value[:max_len]
        return json.dumps(value, default=str)[:max_len]
    except Exception:
        return str(value)[:max_len]


class PipelineDebugger:
    def __init__(self, slow_threshold_ms: float = 2000.0):
        self.redis: aioredis.Redis | None = None
        self._sessions: dict[str, PipelineDebugSession] = {}
        self.slow_threshold_ms = slow_threshold_ms
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def start_session(self, pipeline_name: str) -> PipelineDebugSession:
        session = PipelineDebugSession(
            session_id=str(uuid.uuid4())[:8],
            pipeline_name=pipeline_name,
        )
        self._sessions[session.session_id] = session
        log.debug(f"Debug session started: [{session.session_id}] {pipeline_name}")
        return session

    @asynccontextmanager
    async def step(
        self,
        session_id: str,
        name: str,
        input_val: Any = None,
        metadata: dict | None = None,
    ) -> AsyncIterator[dict]:
        """Context manager that captures a pipeline step."""
        session = self._sessions.get(session_id)
        if not session:
            yield {}
            return

        t0 = time.time()
        ctx: dict = {"output": None, "skip": False}
        error = ""
        status = "ok"

        try:
            yield ctx
        except Exception as e:
            error = traceback.format_exc()
            status = "error"
            log.error(f"Pipeline step '{name}' failed: {e}")
            raise
        finally:
            duration_ms = (time.time() - t0) * 1000
            if ctx.get("skip"):
                status = "skipped"
            elif duration_ms > self.slow_threshold_ms and status == "ok":
                status = "slow"
                log.warning(f"Slow step '{name}': {duration_ms:.0f}ms")

            record = StepRecord(
                step_id=str(uuid.uuid4())[:6],
                name=name,
                input_repr=_repr(input_val),
                output_repr=_repr(ctx.get("output")),
                duration_ms=duration_ms,
                status=status,
                error=error[:500] if error else "",
                metadata=metadata or {},
            )
            session.steps.append(record)

    def record_step(
        self,
        session_id: str,
        name: str,
        input_val: Any,
        output_val: Any,
        duration_ms: float,
        error: str = "",
    ) -> StepRecord:
        """Manual step recording (non-context-manager)."""
        session = self._sessions.get(session_id)
        if not session:
            raise ValueError(f"Session '{session_id}' not found")
        status = (
            "error"
            if error
            else ("slow" if duration_ms > self.slow_threshold_ms else "ok")
        )
        record = StepRecord(
            step_id=str(uuid.uuid4())[:6],
            name=name,
            input_repr=_repr(input_val),
            output_repr=_repr(output_val),
            duration_ms=duration_ms,
            status=status,
            error=error[:500],
        )
        session.steps.append(record)
        return record

    def end_session(
        self, session_id: str, status: str = "done"
    ) -> PipelineDebugSession:
        session = self._sessions.get(session_id)
        if not session:
            raise ValueError(f"Session '{session_id}' not found")
        session.status = status
        session.ended_at = time.time()
        self._save(session)
        log.info(
            f"Debug session done: [{session_id}] {len(session.steps)} steps, "
            f"{session.total_ms:.0f}ms, {len(session.errors)} errors"
        )
        return session

    def _save(self, session: PipelineDebugSession):
        path = DEBUG_DIR / f"{session.session_id}.json"
        path.write_text(
            json.dumps(
                {**session.to_dict(), "steps": [s.to_dict() for s in session.steps]},
                indent=2,
            )
        )

    def report(self, session_id: str) -> str:
        session = self._sessions.get(session_id)
        if not session:
            return f"Session '{session_id}' not found"

        lines = [
            f"Pipeline: {session.pipeline_name} [{session.session_id}]",
            f"Status: {session.status} | Steps: {len(session.steps)} | Total: {session.total_ms:.0f}ms",
            f"Errors: {len(session.errors)}",
            "",
            f"{'Step':<30} {'Status':<10} {'ms':>8}",
            "-" * 55,
        ]
        for s in session.steps:
            flag = " ⚠" if s.slow else (" ✗" if s.status == "error" else "")
            lines.append(f"  {s.name:<30} {s.status:<10} {s.duration_ms:>7.0f}{flag}")

        bn = session.bottleneck
        if bn:
            pct = round(bn.duration_ms / max(session.total_ms, 1) * 100, 1)
            lines.append(
                f"\nBottleneck: {bn.name} ({bn.duration_ms:.0f}ms = {pct}% of total)"
            )

        if session.errors:
            lines.append("\nErrors:")
            for e in session.errors:
                lines.append(f"  [{e.name}]: {e.error[:100]}")

        return "\n".join(lines)

    def diff_sessions(self, sid_a: str, sid_b: str) -> dict:
        a = self._sessions.get(sid_a)
        b = self._sessions.get(sid_b)
        if not a or not b:
            return {"error": "Session not found"}
        step_names_a = {s.name: s for s in a.steps}
        step_names_b = {s.name: s for s in b.steps}
        all_names = sorted(set(step_names_a) | set(step_names_b))
        diffs = []
        for name in all_names:
            sa = step_names_a.get(name)
            sb = step_names_b.get(name)
            if not sa:
                diffs.append({"step": name, "diff": "only_in_b"})
            elif not sb:
                diffs.append({"step": name, "diff": "only_in_a"})
            else:
                delta_ms = sb.duration_ms - sa.duration_ms
                if abs(delta_ms) > 100:
                    diffs.append({"step": name, "delta_ms": round(delta_ms, 1)})
        return {"session_a": sid_a, "session_b": sid_b, "diffs": diffs}

    def stats(self) -> dict:
        sessions = list(self._sessions.values())
        return {
            "sessions": len(sessions),
            "saved": len(list(DEBUG_DIR.glob("*.json"))),
            "total_steps": sum(len(s.steps) for s in sessions),
            "total_errors": sum(len(s.errors) for s in sessions),
        }


async def main():
    import sys

    dbg = PipelineDebugger()
    await dbg.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        session = dbg.start_session("rag_pipeline")

        async with dbg.step(session.session_id, "embed_query", "What is Redis?") as ctx:
            await asyncio.sleep(0.05)
            ctx["output"] = [0.1, 0.2, 0.3]

        async with dbg.step(
            session.session_id, "vector_search", [0.1, 0.2, 0.3]
        ) as ctx:
            await asyncio.sleep(0.12)
            ctx["output"] = ["doc1", "doc2"]

        async with dbg.step(
            session.session_id, "llm_generate", "context + query"
        ) as ctx:
            await asyncio.sleep(0.8)
            ctx["output"] = "Redis is an in-memory store."

        dbg.end_session(session.session_id)
        print(dbg.report(session.session_id))

    elif cmd == "stats":
        print(json.dumps(dbg.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
