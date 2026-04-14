#!/usr/bin/env python3
"""
jarvis_command_handler — Structured command execution with undo/redo and history
CommandSpec validation, execution pipeline, compensation actions for rollback
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.command_handler")

REDIS_PREFIX = "jarvis:cmd:"


class CommandState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    COMPENSATING = "compensating"
    COMPENSATED = "compensated"
    CANCELLED = "cancelled"


@dataclass
class CommandSpec:
    name: str
    description: str = ""
    required_params: list[str] = field(default_factory=list)
    optional_params: dict[str, Any] = field(default_factory=dict)
    timeout_s: float = 30.0
    idempotent: bool = False
    reversible: bool = False


@dataclass
class CommandRecord:
    command_id: str
    name: str
    params: dict[str, Any]
    state: CommandState = CommandState.PENDING
    result: Any = None
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    duration_ms: float = 0.0
    source: str = ""

    def to_dict(self) -> dict:
        return {
            "command_id": self.command_id,
            "name": self.name,
            "params": self.params,
            "state": self.state.value,
            "result": self.result,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": round(self.duration_ms, 2),
            "source": self.source,
        }


@dataclass
class ExecutionResult:
    success: bool
    command_id: str
    name: str
    data: Any = None
    error: str = ""
    state: CommandState = CommandState.SUCCESS
    duration_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "command_id": self.command_id,
            "name": self.name,
            "data": self.data,
            "error": self.error,
            "state": self.state.value,
            "duration_ms": round(self.duration_ms, 2),
        }


class CommandHandler:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._specs: dict[str, CommandSpec] = {}
        self._executors: dict[str, Callable] = {}
        self._compensators: dict[str, Callable] = {}  # undo handlers
        self._history: list[CommandRecord] = []
        self._max_history = 500
        self._undo_stack: list[CommandRecord] = []
        self._redo_stack: list[CommandRecord] = []
        self._max_undo = 50
        self._stats: dict[str, int] = {
            "executed": 0,
            "success": 0,
            "failed": 0,
            "compensated": 0,
            "undone": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def register(
        self,
        spec: CommandSpec,
        executor: Callable,
        compensator: Callable | None = None,
    ):
        self._specs[spec.name] = spec
        self._executors[spec.name] = executor
        if compensator:
            self._compensators[spec.name] = compensator

    def _validate(self, name: str, params: dict) -> str | None:
        spec = self._specs.get(name)
        if not spec:
            return f"unknown command {name!r}"
        missing = [p for p in spec.required_params if p not in params]
        if missing:
            return f"missing required params: {missing}"
        return None

    async def execute(
        self,
        name: str,
        params: dict | None = None,
        source: str = "",
    ) -> ExecutionResult:
        import secrets

        params = params or {}
        self._stats["executed"] += 1

        err = self._validate(name, params)
        if err:
            return ExecutionResult(
                success=False,
                command_id="",
                name=name,
                error=err,
                state=CommandState.FAILED,
            )

        spec = self._specs[name]
        cmd_id = secrets.token_hex(8)
        record = CommandRecord(
            command_id=cmd_id,
            name=name,
            params=params,
            state=CommandState.RUNNING,
            started_at=time.time(),
            source=source,
        )
        self._history.append(record)
        if len(self._history) > self._max_history:
            self._history.pop(0)

        executor = self._executors[name]
        start = time.time()

        try:
            coro = executor(params)
            if asyncio.iscoroutine(coro):
                data = await asyncio.wait_for(coro, timeout=spec.timeout_s)
            else:
                data = coro

            dur = (time.time() - start) * 1000
            record.state = CommandState.SUCCESS
            record.result = data
            record.finished_at = time.time()
            record.duration_ms = dur

            self._stats["success"] += 1
            log.debug(f"Command {name!r} [{cmd_id}] OK {dur:.0f}ms")

            # Push to undo stack if reversible
            if spec.reversible and name in self._compensators:
                self._undo_stack.append(record)
                if len(self._undo_stack) > self._max_undo:
                    self._undo_stack.pop(0)
                self._redo_stack.clear()

            if self.redis:
                asyncio.create_task(self._redis_store(record))

            return ExecutionResult(
                success=True,
                command_id=cmd_id,
                name=name,
                data=data,
                state=CommandState.SUCCESS,
                duration_ms=dur,
            )

        except asyncio.TimeoutError:
            dur = (time.time() - start) * 1000
            record.state = CommandState.FAILED
            record.error = f"timeout after {spec.timeout_s}s"
            record.finished_at = time.time()
            record.duration_ms = dur
            self._stats["failed"] += 1
            return ExecutionResult(
                success=False,
                command_id=cmd_id,
                name=name,
                error=record.error,
                state=CommandState.FAILED,
                duration_ms=dur,
            )

        except Exception as e:
            dur = (time.time() - start) * 1000
            record.state = CommandState.FAILED
            record.error = str(e)
            record.finished_at = time.time()
            record.duration_ms = dur
            self._stats["failed"] += 1
            log.error(f"Command {name!r} [{cmd_id}] FAILED: {e}")
            return ExecutionResult(
                success=False,
                command_id=cmd_id,
                name=name,
                error=str(e),
                state=CommandState.FAILED,
                duration_ms=dur,
            )

    async def undo(self) -> ExecutionResult | None:
        if not self._undo_stack:
            return None
        record = self._undo_stack.pop()
        compensator = self._compensators.get(record.name)
        if not compensator:
            return None

        record.state = CommandState.COMPENSATING
        start = time.time()
        try:
            coro = compensator(record.params, record.result)
            if asyncio.iscoroutine(coro):
                await coro
            dur = (time.time() - start) * 1000
            record.state = CommandState.COMPENSATED
            self._redo_stack.append(record)
            self._stats["compensated"] += 1
            self._stats["undone"] += 1
            log.info(f"Undone command {record.name!r} [{record.command_id}]")
            return ExecutionResult(
                success=True,
                command_id=record.command_id,
                name=record.name,
                state=CommandState.COMPENSATED,
                duration_ms=dur,
            )
        except Exception as e:
            record.state = CommandState.FAILED
            return ExecutionResult(
                success=False,
                command_id=record.command_id,
                name=record.name,
                error=str(e),
                state=CommandState.FAILED,
            )

    async def redo(self) -> ExecutionResult | None:
        if not self._redo_stack:
            return None
        record = self._redo_stack.pop()
        return await self.execute(record.name, record.params, source="redo")

    async def _redis_store(self, record: CommandRecord):
        if not self.redis:
            return
        try:
            await self.redis.setex(
                f"{REDIS_PREFIX}{record.command_id}",
                3600,
                json.dumps(record.to_dict()),
            )
        except Exception:
            pass

    def history(self, limit: int = 20, name: str | None = None) -> list[dict]:
        entries = self._history
        if name:
            entries = [r for r in entries if r.name == name]
        return [r.to_dict() for r in entries[-limit:]]

    def undo_depth(self) -> int:
        return len(self._undo_stack)

    def redo_depth(self) -> int:
        return len(self._redo_stack)

    def list_commands(self) -> list[dict]:
        return [
            {
                "name": s.name,
                "description": s.description,
                "required": s.required_params,
                "reversible": s.reversible,
                "idempotent": s.idempotent,
            }
            for s in self._specs.values()
        ]

    def stats(self) -> dict:
        return {
            **self._stats,
            "undo_depth": self.undo_depth(),
            "redo_depth": self.redo_depth(),
            "history_size": len(self._history),
            "commands_registered": len(self._specs),
        }


def build_jarvis_command_handler() -> CommandHandler:
    ch = CommandHandler()

    # model.load
    async def exec_model_load(params: dict) -> dict:
        await asyncio.sleep(0.01)  # simulate
        return {
            "loaded": params["model"],
            "node": params.get("node", "m1"),
            "ts": time.time(),
        }

    async def undo_model_load(params: dict, result: Any):
        log.info(f"Unloading model {params['model']} from {params.get('node', 'm1')}")

    ch.register(
        CommandSpec(
            "model.load",
            "Load a model onto a node",
            ["model"],
            {"node": "m1"},
            reversible=True,
        ),
        exec_model_load,
        undo_model_load,
    )

    # model.unload
    async def exec_model_unload(params: dict) -> dict:
        return {"unloaded": params["model"], "node": params.get("node", "m1")}

    ch.register(
        CommandSpec(
            "model.unload", "Unload a model from a node", ["model"], {"node": "m1"}
        ),
        exec_model_unload,
    )

    # agent.restart
    async def exec_agent_restart(params: dict) -> dict:
        await asyncio.sleep(0.01)
        return {"agent": params["agent_id"], "restarted": True}

    ch.register(
        CommandSpec("agent.restart", "Restart an agent", ["agent_id"]),
        exec_agent_restart,
    )

    # config.set
    async def exec_config_set(params: dict) -> dict:
        return {"key": params["key"], "value": params["value"], "applied": True}

    async def undo_config_set(params: dict, result: Any):
        log.info(f"Reverting config {params['key']}")

    ch.register(
        CommandSpec(
            "config.set",
            "Set a config value",
            ["key", "value"],
            reversible=True,
            idempotent=True,
        ),
        exec_config_set,
        undo_config_set,
    )

    return ch


async def main():
    import sys

    ch = build_jarvis_command_handler()
    await ch.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Command handler demo...")

        r1 = await ch.execute("model.load", {"model": "qwen3.5-9b", "node": "m1"})
        r2 = await ch.execute("config.set", {"key": "log_level", "value": "debug"})
        r3 = await ch.execute("agent.restart", {"agent_id": "inference-gw"})
        r4 = await ch.execute(
            "model.load", {"model": "missing_required"}
        )  # validation fail: no model? no — model is provided; try unknown command
        r5 = await ch.execute("unknown.cmd", {})

        for r in [r1, r2, r3, r4, r5]:
            icon = "✅" if r.success else "❌"
            print(f"  {icon} {r.name:<20} → {r.data or r.error}")

        print(f"\nUndo depth: {ch.undo_depth()}")
        u = await ch.undo()
        if u:
            print(f"  Undone: {u.name} [{u.state.value}]")
        u2 = await ch.undo()
        if u2:
            print(f"  Undone: {u2.name} [{u2.state.value}]")

        re = await ch.redo()
        if re:
            print(f"  Redone: {re.name} [{re.state.value}]")

        print(f"\nStats: {json.dumps(ch.stats(), indent=2)}")

    elif cmd == "list":
        print(json.dumps(ch.list_commands(), indent=2))

    elif cmd == "stats":
        print(json.dumps(ch.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
